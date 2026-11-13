package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"ycluster.local/ids-forwarder/sigma"
)

const sourceSigma = "sigma"

// sigmaDetection is the subset of RSigma's EvaluationResult that the
// forwarder turns back into an IDSAlert. The event is present because the
// daemon runs with --include-event.
type sigmaDetection struct {
	RuleTitle string          `json:"rule_title"`
	RuleID    string          `json:"rule_id"`
	Level     string          `json:"level"`
	Tags      []string        `json:"tags"`
	Event     json.RawMessage `json:"event"`
}

type sigmaCursorState struct {
	Device uint64 `json:"device"`
	Inode  uint64 `json:"inode"`
	Offset int64  `json:"offset"`
}

// startSigmaDetections tails the RSigma file sink. The file is append-only
// and low volume; a byte cursor lets a restart resume after the last
// processed detection instead of replaying the whole history. If the file
// was rotated while the forwarder was down, the new file is read from the
// start and duplicate alerts are handled by the normal dedup window.
func (f *forwarder) startSigmaDetections() error {
	path := f.cfg.sigmaDetections
	cursorPath := f.cfg.sigmaCursorFile
	if cursorPath == "" {
		cursorPath = path + ".cursor"
	}
	state, haveState, err := loadSigmaCursor(cursorPath)
	if err != nil {
		f.cfg.logf("sigma: read cursor: %v; replaying detections file", err)
	}
	info, statErr := os.Stat(path)
	exists := statErr == nil
	if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
		return fmt.Errorf("stat sigma detections %s: %w", path, statErr)
	}
	var start int64
	switch {
	case exists && haveState && sameIdentity(state.Device, state.Inode, info) && state.Offset <= info.Size():
		start = state.Offset
	case exists && haveState:
		start = 0
	case exists:
		// First start with a pre-existing file: its lines predate this
		// forwarder, so do not page them as new detections.
		start = info.Size()
	}

	handler := f.sigmaLineHandler()
	var tl *tailer
	if exists {
		file, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("open sigma detections %s: %w", path, err)
		}
		tl, err = newTailerFromFile(file, path, f.cfg.pollInterval, start, handler)
		if err != nil {
			return fmt.Errorf("tail sigma detections %s: %w", path, err)
		}
	} else {
		// The sink is created with the first detection; tail from its
		// beginning once it appears.
		tl = newRetryTailer(path, f.cfg.pollInterval, handler)
	}
	tl.onProgress = func(dev, ino uint64, offset int64) {
		f.saveSigmaCursor(cursorPath, dev, ino, offset)
	}
	f.sigmaTail = tl
	f.wg.Add(1)
	go func() {
		defer f.wg.Done()
		tl.run()
	}()
	return nil
}

// sigmaLineHandler maps one detection result to an alert and enqueues it
// through the same durable spool as the hand-written rules.
func (f *forwarder) sigmaLineHandler() func([]byte) {
	return func(line []byte) {
		if f.stopping() {
			return
		}
		a, ok := sigmaDetectionToAlert(string(line), f.cfg)
		if !ok {
			f.stats.sigmaBad.Add(1)
			f.cfg.logf("sigma: unparsable detection: %s", boundSummary(string(line)))
			return
		}
		if a.tooOld(time.Now(), f.cfg.maxEventAge) {
			f.stats.expired.Add(1)
			return
		}
		f.stats.countProvenance(a.prov)
		if err := f.enqueue(a); err != nil {
			f.cfg.logf("fatal: sigma detection enqueue: %v", err)
			go f.fail(err)
		}
	}
}

func sigmaDetectionToAlert(line string, cfg config) (alert, bool) {
	var det sigmaDetection
	if err := json.Unmarshal([]byte(line), &det); err != nil || det.RuleTitle == "" {
		return alert{}, false
	}
	var ev sigma.Event
	if len(det.Event) > 0 {
		_ = json.Unmarshal(det.Event, &ev)
	}
	rule := det.RuleID
	if rule == "" {
		rule = sigmaRuleSlug(det.RuleTitle)
	}
	parts := []string{"sigma=" + det.RuleTitle}
	if ev.Image != "" {
		parts = append(parts, "image="+ev.Image)
	}
	if ev.CommandLine != "" {
		parts = append(parts, "args="+ev.CommandLine)
	}
	if ev.ParentImage != "" {
		parts = append(parts, "parent="+ev.ParentImage)
	}
	if ev.ContainerName != "" {
		parts = append(parts, "container="+ev.ContainerName)
	}
	if ev.User != "" {
		parts = append(parts, "user="+ev.User)
	}
	if ev.DestinationIp != "" {
		parts = append(parts, fmt.Sprintf("dst=%s:%d", ev.DestinationIp, ev.DestinationPort))
	}
	layer := sigmaLayer(ev)
	containerName := ev.ContainerName
	if layer == "host" {
		if r := resolveProcessLayer(ev.ProcessId, ev.StartTime, sigmaUID(ev.User), sigmaNamespaces(ev.Ns)); r.Resolved && r.Layer != "" {
			layer = r.Layer
			if r.Name != "" {
				containerName = r.Name
			}
		}
	}
	a := newAlert(cfg, sourceSigma, sigmaSeverity(det.Level), layer, rule,
		redactIPs(strings.Join(parts, " ")))
	// The visible summary redacts addresses; keep the unredacted event as the
	// dedup identity so callbacks to different hosts do not collapse.
	a.dedupIdentity = identityHash(det.RuleID + "|" + strings.Join(parts, " "))
	if containerName != "" {
		a.Labels["container"] = containerName
	}
	applySigmaIdentity(&a, ev, layer)
	// Keep the engine's own level alongside the two-level cluster severity:
	// paging decides on "medium or above", which the mapped severity cannot
	// express (low and medium both map to warning).
	a.Labels["sigma_level"] = strings.ToLower(strings.TrimSpace(det.Level))
	if t, err := time.Parse(time.RFC3339Nano, ev.Timestamp); err == nil {
		a.StartsAt = t.UTC().Format(time.RFC3339Nano)
		a.eventTime = t.UTC()
	}
	a.eventHash = lineHash(line)
	return a, true
}

// sigmaNamespaces converts the rule layer's copy of the namespace snapshot.
// A detection echoed back by RSigma carries none: the normalized event it
// stores has no ns field, which is why this path stays best-effort.
func sigmaNamespaces(ns *sigma.Namespaces) *eventNamespaces {
	if ns == nil {
		return nil
	}
	return &eventNamespaces{
		Pid: tetragonNamespace{Inum: ns.Pid.Inum, IsHost: ns.Pid.IsHost},
		Mnt: tetragonNamespace{Inum: ns.Mnt.Inum, IsHost: ns.Mnt.IsHost},
	}
}

// sigmaUID parses the normalized User field, which is "root" or a numeric uid.
func sigmaUID(user string) uint32 {
	switch {
	case user == "root":
		return 0
	case user == "":
		return 0
	}
	value, err := strconv.ParseUint(user, 10, 32)
	if err != nil {
		return 0
	}
	return uint32(value)
}

// applySigmaIdentity gives a Sigma detection the same provenance the Tetragon
// path carries, so the delivery classifier can tell a root host action from a
// tenant one. The evaluator already supplies the uid as a user name and the
// pid with its start time, which is what the cgroup lookup needs.
func applySigmaIdentity(a *alert, ev sigma.Event, layer string) {
	switch ev.User {
	case "":
	case "root":
		a.Labels["uid"] = "0"
	default:
		a.Labels["uid"] = ev.User
	}
	a.Labels["kind"] = "sigma"
	switch layer {
	case "container":
		a.Labels["origin"] = "container"
	case "unverified":
		a.Labels["origin"] = "unverified"
	default:
		o := processOriginFor(ev.ProcessId, ev.StartTime)
		a.Labels["origin"] = o.Origin
		if o.Actor != "" {
			a.Labels["actor"] = redactIPs(o.Actor)
		}
	}
	// The normalized Sigma event carries no parent uid, so the audit uid is
	// the first invoker evidence here and the session the last.
	if privilegeTools[ev.Image] && layer != "container" {
		if source := sigmaEscalationSource(ev); source != "" {
			a.Labels["escalation"] = "nonroot"
			a.prov.escalation = source
		}
	}
}

// sigmaEscalationSource mirrors escalationSource without a parent uid: the
// audit uid is carried on the event, the session is read from /proc.
func sigmaEscalationSource(ev sigma.Event) string {
	if ev.Auid != auidUnset && ev.Auid >= loginUIDBase && ev.Auid < incusIDMapBase {
		return "auid"
	}
	if nonRootSession(ev.ProcessId, ev.StartTime) {
		return "session"
	}
	return ""
}

// sigmaSeverity keeps the two-level cluster alert vocabulary. High and
// critical Sigma levels page; everything else is a warning.
func sigmaSeverity(level string) string {
	switch strings.ToLower(strings.TrimSpace(level)) {
	case "critical", "high":
		return "critical"
	default:
		return "warning"
	}
}

func sigmaLayer(ev sigma.Event) string {
	if ev.Layer != "" {
		return ev.Layer
	}
	if ev.ContainerName != "" {
		return "container"
	}
	return "host"
}

func sigmaRuleSlug(title string) string {
	var b strings.Builder
	lastDash := false
	for _, r := range strings.ToLower(title) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
			lastDash = false
		default:
			if b.Len() > 0 && !lastDash {
				b.WriteByte('-')
				lastDash = true
			}
		}
	}
	slug := strings.Trim(b.String(), "-")
	if slug == "" {
		return "sigma"
	}
	return slug
}

func loadSigmaCursor(path string) (sigmaCursorState, bool, error) {
	var state sigmaCursorState
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return state, false, nil
	}
	if err != nil {
		return state, false, err
	}
	if err := json.Unmarshal(data, &state); err != nil {
		return sigmaCursorState{}, false, err
	}
	if state.Inode == 0 {
		return sigmaCursorState{}, false, errors.New("invalid sigma cursor")
	}
	return state, true, nil
}

func (f *forwarder) saveSigmaCursor(path string, dev, ino uint64, offset int64) {
	if err := writeSigmaCursor(path, sigmaCursorState{Device: dev, Inode: ino, Offset: offset}); err != nil {
		f.stats.cursorErrors.Add(1)
		f.cfg.logf("sigma: save cursor: %v", err)
	}
}

func writeSigmaCursor(path string, state sigmaCursorState) error {
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o640); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
