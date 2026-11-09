package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

// sessionDir is systemd's login-session registry. Only the login user is
// read from it: each record also carries the peer address, which must not
// reach a label or the detection log.
var sessionDir = "/run/systemd/sessions"

const sessionCacheTTL = 10 * time.Minute

// processOrigin says which login session or systemd unit started a host
// process. It is provenance for labels and counting only; no classification
// suppresses or pages on the strength of it.
type processOrigin struct {
	Origin string // interactive, unit, container or unknown
	Actor  string // login user or unit name, empty when unknown
}

// eventOrigin resolves the origin of an event's process, falling back to the
// parent when the child has already exited, the same way container
// attribution does.
func eventOrigin(process tetragonProcess, parent *tetragonProc) processOrigin {
	if o := processOriginFor(process.PID, process.StartTime); o.Origin != "unknown" {
		return o
	}
	if parent != nil {
		if o := processOriginFor(parent.PID, parent.StartTime); o.Origin != "unknown" {
			return o
		}
	}
	return processOrigin{Origin: "unknown"}
}

func processOriginFor(pid uint32, startTime string) processOrigin {
	path, ok := cgroupPath(pid, startTime)
	if !ok {
		return processOrigin{Origin: "unknown"}
	}
	// A container has its own session scopes inside the payload cgroup; the
	// numbers mean nothing on the host, so never resolve them here.
	if _, inContainer := containerNameFromCgroup(path); inContainer {
		return processOrigin{Origin: "container"}
	}
	if n, ok := loginSessionID(path); ok {
		if user := sessionUser(n); user != "" {
			return processOrigin{Origin: "interactive", Actor: user}
		}
		return processOrigin{Origin: "interactive"}
	}
	if unit, ok := unitName(path); ok {
		return processOrigin{Origin: "unit", Actor: unit}
	}
	return processOrigin{Origin: "unknown"}
}

// loginSessionID returns the session number of a cgroup path such as
// 0::/user.slice/user-0.slice/session-2176.scope. A login session is the
// unit of provenance for interactive work: children inherit the scope, and
// cron or a systemd unit never lands in one.
func loginSessionID(cgroup string) (string, bool) {
	for _, part := range strings.Split(cgroup, "/") {
		part = strings.TrimSpace(part)
		if !strings.HasPrefix(part, "session-") || !strings.HasSuffix(part, ".scope") {
			continue
		}
		id := strings.TrimSuffix(strings.TrimPrefix(part, "session-"), ".scope")
		if id == "" || strings.Trim(id, "0123456789") != "" {
			continue
		}
		return id, true
	}
	return "", false
}

// unitName returns the systemd unit that owns a process, either a system
// unit or a user manager. Slices are skipped on the way down, so a unit
// nested in system-getty.slice reports getty@tty1 rather than the slice, and
// the suffix is dropped so a unit reads the same whichever type it is.
func unitName(cgroup string) (string, bool) {
	for _, line := range strings.Split(cgroup, "\n") {
		i := strings.Index(line, "::")
		if i < 0 {
			continue
		}
		inHierarchy := false
		unit := ""
		for _, raw := range strings.Split(line[i+2:], "/") {
			part := strings.TrimSpace(raw)
			switch {
			case part == "":
			case part == "system.slice", part == "user.slice", part == "machine.slice":
				inHierarchy = true
			case !inHierarchy:
			case strings.HasSuffix(part, ".slice"):
				// A slice groups units; the unit it contains is the name
				// worth reporting.
			case unitSuffix(part) != "":
				unit = trimUnitSuffix(part)
			}
		}
		if !inHierarchy {
			continue
		}
		if strings.HasPrefix(unit, "session-") {
			// A login session is handled by loginSessionID; it is not a
			// unit anyone would look up by name.
			return "", false
		}
		if transientUnit(unit) {
			// run-r<hex>.scope units are unique per systemd-run; a label
			// per invocation would grow without bound, so only the origin
			// is kept.
			return "", true
		}
		return unit, true
	}
	return "", false
}

var unitSuffixes = []string{".service", ".scope", ".socket", ".timer", ".mount", ".automount", ".target", ".path", ".swap", ".device"}

func unitSuffix(part string) string {
	for _, suffix := range unitSuffixes {
		if strings.HasSuffix(part, suffix) {
			return suffix
		}
	}
	return ""
}

func trimUnitSuffix(unit string) string {
	for _, suffix := range unitSuffixes {
		if strings.HasSuffix(unit, suffix) {
			return strings.TrimSuffix(unit, suffix)
		}
	}
	return unit
}

func transientUnit(unit string) bool {
	return strings.HasPrefix(unit, "run-r")
}

type sessionEntry struct {
	user    string
	expires time.Time
}

var (
	sessionMu    sync.Mutex
	sessionUsers = map[string]sessionEntry{}
)

// sessionUser maps a session number to its login user. Sessions are few and
// long-lived, so the map is a short TTL cache rather than an LRU.
func sessionUser(id string) string {
	now := time.Now()
	sessionMu.Lock()
	if e, ok := sessionUsers[id]; ok && now.Before(e.expires) {
		sessionMu.Unlock()
		return e.user
	}
	sessionMu.Unlock()

	user := readSessionUser(id)
	sessionMu.Lock()
	if len(sessionUsers) > 256 {
		for k, e := range sessionUsers {
			if now.After(e.expires) {
				delete(sessionUsers, k)
			}
		}
	}
	sessionUsers[id] = sessionEntry{user: user, expires: now.Add(sessionCacheTTL)}
	sessionMu.Unlock()
	return user
}

func readSessionUser(id string) string {
	data, err := os.ReadFile(fmt.Sprintf("%s/%s", sessionDir, id))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		value, ok := strings.CutPrefix(line, "USER=")
		if !ok {
			continue
		}
		if value = strings.TrimSpace(value); value != "" && !strings.ContainsAny(value, "\"\\ ") {
			return value
		}
	}
	return ""
}
