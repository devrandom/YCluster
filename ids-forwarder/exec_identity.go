package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"ycluster.local/ids-forwarder/sigma"
)

// Tetragon reports a process it never saw exec as the init binary with a
// placeholder argument. Services that start before the sensor has attached,
// notably after a reboot, keep that identity for their lifetime and every
// connect they make is attributed to systemd.
const (
	unknownExecBinary = "/usr/lib/systemd/systemd"
	unknownExecArg    = "text"
)

// unknownExec reports whether an event carries that placeholder identity.
func unknownExec(binary, args string) bool {
	return binary == unknownExecBinary && strings.TrimSpace(args) == unknownExecArg
}

// resolveEventIdentity replaces the placeholder with what /proc reports for
// the running process. The event carries the pid and its exec time, and the
// start-time check rejects a pid that has been reused since, so an old event
// cannot inherit another process's identity.
func resolveEventIdentity(process *tetragonProcess) bool {
	if process == nil || !unknownExec(process.Binary, process.Arguments) {
		return false
	}
	binary, args, ok := resolveProcessIdentity(process.PID, process.StartTime)
	if !ok {
		return false
	}
	process.Binary = binary
	process.Arguments = args
	return true
}

// procSnapshotEntry is one process captured at startup.
type procSnapshotEntry struct {
	start  string
	binary string
	args   string
}

var (
	snapshotMu sync.Mutex
	snapshot   map[uint32]procSnapshotEntry
	memoMu     sync.Mutex
	memo       map[uint32]procSnapshotEntry
)

// captureProcessSnapshot remembers every process alive at startup. Tetragon
// reports a process it never saw exec as the init binary, and the services
// that shape covers are exactly the long-lived ones; reading them at startup
// is the only moment they are guaranteed to still be in /proc for the whole
// burst of events that follows a restart.
// captureProcessSnapshot returns the number of processes it captured. A
// failure is logged by the caller: the forwarder still works through the
// live /proc path, but on a node where /proc reads are restricted the
// snapshot would otherwise be silently inert.
func captureProcessSnapshot(logf func(string, ...any)) int {
	entries, err := os.ReadDir(procRoot)
	if err != nil {
		if logf != nil {
			logf("process snapshot: read %s: %v; pre-attach identities will resolve from live /proc only", procRoot, err)
		}
		return 0
	}
	snap := make(map[uint32]procSnapshotEntry, len(entries))
	for _, entry := range entries {
		value, err := strconv.ParseUint(entry.Name(), 10, 32)
		if err != nil {
			continue
		}
		pid := uint32(value)
		base := fmt.Sprintf("%s/%d", procRoot, pid)
		start, ok := readProcessStart(base)
		if !ok {
			continue
		}
		binary, args, ok := readCmdline(base)
		if !ok {
			continue
		}
		snap[pid] = procSnapshotEntry{start: start, binary: binary, args: args}
	}
	snapshotMu.Lock()
	snapshot = snap
	snapshotMu.Unlock()
	if len(snap) == 0 && logf != nil {
		logf("process snapshot captured no processes; pre-attach identities will resolve from live /proc")
	}
	return len(snap)
}

// resetProcessSnapshot clears both caches. Tests use it after pointing
// procRoot at a fake tree.
func resetProcessSnapshot() {
	snapshotMu.Lock()
	snapshot = nil
	snapshotMu.Unlock()
	memoMu.Lock()
	memo = nil
	memoMu.Unlock()
}

func snapshotLookup(pid uint32, startTime string) (procSnapshotEntry, bool) {
	snapshotMu.Lock()
	defer snapshotMu.Unlock()
	entry, ok := snapshot[pid]
	if !ok || !startTimeWithin(entry.start, startTime) {
		return procSnapshotEntry{}, false
	}
	return entry, true
}

// memoCap bounds the replay memo so a long backfill cannot grow it without
// limit.
const memoCap = 1024

func memoLookup(pid uint32, startTime string) (procSnapshotEntry, bool) {
	memoMu.Lock()
	defer memoMu.Unlock()
	entry, ok := memo[pid]
	if !ok || !startTimeWithin(entry.start, startTime) {
		return procSnapshotEntry{}, false
	}
	return entry, true
}

func memoStore(pid uint32, start, binary, args string) {
	memoMu.Lock()
	defer memoMu.Unlock()
	if memo == nil || len(memo) >= memoCap {
		memo = map[uint32]procSnapshotEntry{}
	}
	memo[pid] = procSnapshotEntry{start: start, binary: binary, args: args}
}

// resolveProcessIdentity reads the command line from the startup snapshot,
// then from a bounded replay memo, and only then from live /proc.
func resolveProcessIdentity(pid uint32, startTime string) (string, string, bool) {
	if pid == 0 || startTime == "" {
		return "", "", false
	}
	if entry, ok := snapshotLookup(pid, startTime); ok {
		return entry.binary, entry.args, true
	}
	if entry, ok := memoLookup(pid, startTime); ok {
		return entry.binary, entry.args, true
	}
	base := fmt.Sprintf("%s/%d", procRoot, pid)
	if !processStartMatches(base, startTime) {
		return "", "", false
	}
	binary, args, ok := readCmdline(base)
	if !ok {
		return "", "", false
	}
	memoStore(pid, startTime, binary, args)
	return binary, args, true
}

// readCmdline reproduces Tetragon's binary/arguments split from a live
// process, including the interpreter-name quirk the suppression table expects.
func readCmdline(base string) (string, string, bool) {
	raw, err := os.ReadFile(base + "/cmdline")
	if err != nil {
		return "", "", false
	}
	argv := strings.Split(strings.TrimRight(string(raw), "\x00"), "\x00")
	if len(argv) == 0 || argv[0] == "" {
		return "", "", false
	}
	binary := argv[0]
	if exe, err := os.Readlink(base + "/exe"); err == nil {
		binary = strings.TrimSuffix(exe, " (deleted)")
	}
	// Tetragon names the interpreter as the binary and starts the arguments
	// at the script; reproduce that so the suppression table still matches.
	if isPython(filepath.Base(argv[0])) && len(argv) > 1 {
		argv = argv[1:]
	}
	return binary, strings.Join(argv, " "), true
}

// resolveSigmaIdentity applies the same fallback to the rule-layer copy, which
// is normalized separately from the alert path.
func resolveSigmaIdentity(ev *sigma.Event) {
	if ev == nil {
		return
	}
	if ev.Layer == "" {
		ev.Layer = "host"
	}
	if ev.Layer == "host" {
		// A process that /proc still knows is classified by its cgroup. Only
		// when both it and its parent are gone does the uid heuristic apply;
		// otherwise every non-root host service would lose its host
		// exclusions.
		r := resolveProcessLayer(ev.ProcessId, ev.StartTime, sigmaUID(ev.User), sigmaNamespaces(ev.Ns))
		if !r.Resolved && ev.ParentProcessId != 0 {
			r = resolveProcessLayer(ev.ParentProcessId, ev.ParentStartTime, ev.ParentUID, sigmaNamespaces(ev.ParentNs))
		}
		switch {
		case r.Resolved:
			if r.Layer != "" {
				ev.Layer = r.Layer
				if r.Name != "" && ev.ContainerName == "" {
					ev.ContainerName = r.Name
				}
			}
		case incusMappedUser(ev.User):
			ev.Layer = "container"
		case loginUIDUser(ev.User):
			ev.Layer = "unverified"
		}
	}
	if ev.Image != unknownExecBinary ||
		!strings.HasSuffix(strings.TrimSpace(ev.CommandLine), " "+unknownExecArg) {
		return
	}
	binary, args, ok := resolveProcessIdentity(ev.ProcessId, ev.StartTime)
	if !ok {
		return
	}
	ev.Image = binary
	ev.CommandLine = strings.TrimSpace(binary + " " + args)
}

func incusMappedUser(user string) bool {
	uid, err := strconv.ParseUint(user, 10, 32)
	return err == nil && uid >= incusIDMapBase
}

// loginUIDUser reports whether the numeric uid belongs to a login account.
// System accounts below loginUIDBase are host services even when the process
// has already exited.
func loginUIDUser(user string) bool {
	uid, err := strconv.ParseUint(user, 10, 32)
	return err == nil && uid >= loginUIDBase
}
