package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
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
	a := newAlert(cfg, sourceSigma, sigmaSeverity(det.Level), sigmaLayer(ev), rule,
		redactIPs(strings.Join(parts, " ")))
	if t, err := time.Parse(time.RFC3339Nano, ev.Timestamp); err == nil {
		a.StartsAt = t.UTC().Format(time.RFC3339Nano)
		a.eventTime = t.UTC()
	}
	a.eventHash = lineHash(line)
	return a, true
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
