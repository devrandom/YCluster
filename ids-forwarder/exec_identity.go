package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

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

// resolveProcessIdentity reads the live command line from /proc.
func resolveProcessIdentity(pid uint32, startTime string) (string, string, bool) {
	if pid == 0 || startTime == "" {
		return "", "", false
	}
	base := fmt.Sprintf("%s/%d", procRoot, pid)
	if !processStartMatches(base, startTime) {
		return "", "", false
	}
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
	if ev == nil || ev.Image != unknownExecBinary ||
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
