package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// procRoot is a variable so tests can point the container lookup at a fake
// proc tree.
var procRoot = "/proc"

// containerForProcess reports whether pid belongs to a container and, when
// the cgroup carries a name (Incus/LXC), returns it. The event's process
// start time must match /proc or the pid has been reused since the event was
// written, which is common during replay.
func containerForProcess(pid uint32, startTime string) (string, bool) {
	name, layer := containerForProcessLayer(pid, startTime)
	return name, layer == "container"
}

// processLayer is the outcome of resolving one process to a layer. Resolved
// separates a confirmed host process from one /proc no longer knows: both
// report an empty Layer, but only the unresolved one may fall back to the uid
// heuristic. Without that distinction every non-root host service would be
// treated as an unverified container.
type processLayer struct {
	Name     string
	Layer    string
	Resolved bool
}

// resolveProcessLayer returns the container layer ("container" or
// "unverified") and, for a named LXC/Incus payload, its name. A user-scope
// runtime is "unverified": it may be a real rootless container, but a user can
// create the same cgroup name, so it must not inherit host or container
// suppression.
func resolveProcessLayer(pid uint32, startTime string) processLayer {
	cgroup, ok := cgroupPath(pid, startTime)
	if !ok {
		return processLayer{}
	}
	name, inContainer := containerNameFromCgroup(cgroup)
	if !inContainer {
		return processLayer{Resolved: true}
	}
	if userScopeRuntime(cgroup) {
		return processLayer{Layer: "unverified", Resolved: true}
	}
	return processLayer{Name: name, Layer: "container", Resolved: true}
}

// containerForProcessLayer keeps the two-value shape for callers that only
// need the container verdict; resolveProcessLayer carries the host/unknown
// distinction.
func containerForProcessLayer(pid uint32, startTime string) (string, string) {
	r := resolveProcessLayer(pid, startTime)
	return r.Name, r.Layer
}

// cgroupPath returns the process's cgroup file, guarding against pid reuse
// the same way container attribution does: /proc must belong to the process
// the event named, not to whatever has the pid now.
func cgroupPath(pid uint32, startTime string) (string, bool) {
	if pid == 0 || startTime == "" {
		return "", false
	}
	base := fmt.Sprintf("%s/%d", procRoot, pid)
	data, err := os.ReadFile(base + "/cgroup")
	if err != nil {
		return "", false
	}
	if !processStartMatches(base, startTime) {
		return "", false
	}
	return string(data), true
}

// containerForEvent attributes an event to a container. Short-lived children
// (motd scripts, shell probes) are often gone by the time the line is read, so
// when the process itself no longer resolves, fall back to its parent, which
// is normally the long-lived process the container started.
func containerForEvent(body *tetragonLog) (string, bool) {
	name, layer := containerForEventLayer(body)
	return name, layer == "container"
}

func containerForEventLayer(body *tetragonLog) (string, string) {
	r := resolveEventLayer(body)
	return r.Name, r.Layer
}

// resolveEventLayer attributes an event's process, then its parent when the
// process is gone. The uid fallback only runs when /proc knows neither, so a
// resolved host process is never reclassified by its uid.
func resolveEventLayer(body *tetragonLog) processLayer {
	if body == nil {
		return processLayer{}
	}
	if r := resolveProcessLayer(body.Process.PID, body.Process.StartTime); r.Resolved {
		return r
	}
	if p := body.Parent; p != nil {
		if r := resolveProcessLayer(p.PID, p.StartTime); r.Resolved {
			return r
		}
	}
	// Both processes can be gone by the time the line is read. Incus maps
	// container accounts into a host uid range that no host account uses
	// here: host accounts top out at 65534, containers start at 1,000,000.
	// The uid is in the event, so attribution does not depend on /proc. Keep
	// the base at Incus's configured idmap rather than the general subuid
	// base (100000): otherwise a user namespace with an ordinary subuid range
	// could claim the container layer without any container runtime.
	if body.Process.UID >= incusIDMapBase {
		return processLayer{Layer: "container"}
	}
	if p := body.Parent; p != nil && p.UID >= incusIDMapBase {
		return processLayer{Layer: "container"}
	}
	// Both processes are gone and neither is idmapped. A dead login-uid
	// process can be a rootless container whose cgroup is no longer in /proc,
	// so it must not inherit host automation or host-only filters. System
	// accounts stay host: package helpers run as `_apt` (42) or `pcp` (996),
	// they exit before the line is read, and no rootless runtime starts from a
	// system uid.
	if body.Process.UID >= loginUIDBase {
		return processLayer{Layer: "unverified"}
	}
	if p := body.Parent; p != nil && p.UID >= loginUIDBase {
		return processLayer{Layer: "unverified"}
	}
	return processLayer{}
}

// loginUIDBase is the first uid a human login account gets on these hosts.
// Rootless container runtimes run as the invoking login user, so a dead
// process at or above this uid cannot be assumed to be a host service.
const loginUIDBase = 1000

// incusIDMapBase is the lowest host uid the Incus idmap can produce. It is
// below the cluster's 1,000,000 base so a different idmap range still
// matches, and above every host account (nobody is 65534).
const incusIDMapBase = 1000000

// containerNameFromCgroup recognises the cgroup paths used by Incus/LXC,
// Docker/Podman and Kubernetes. Only LXC-style paths carry a name. Match
// whole cgroup components and real runtime roots: a scope named
// `docker-evil` or a user cgroup containing `docker-` is a host process, not
// a container, and the attacker can choose that text.
func containerNameFromCgroup(cgroup string) (string, bool) {
	for _, line := range strings.Split(cgroup, "\n") {
		i := strings.Index(line, "::")
		if i < 0 {
			continue
		}
		path := strings.Trim(line[i+2:], "/")
		if path == "" {
			continue
		}
		parts := strings.Split(path, "/")
		if n := lxcName(parts); n != "" {
			return n, true
		}
		if runtimeComponent(parts) >= 0 {
			return "", true
		}
	}
	return "", false
}

// lxcName accepts only the root-level LXC cgroup layouts: v2
// lxc.payload.<name> and v1 lxc/<name>. A nested component with that name
// belongs to something else.
func lxcName(parts []string) string {
	if len(parts) == 0 {
		return ""
	}
	if name := strings.TrimPrefix(parts[0], "lxc.payload."); name != parts[0] && name != "" {
		return name
	}
	if parts[0] == "lxc" && len(parts) > 1 && parts[1] != "" {
		return parts[1]
	}
	return ""
}

// runtimeComponent reports the cgroup component that names a real container
// runtime, or -1. Docker and Podman use a 64-hex container id; a user-created
// scope such as docker-evil.scope does not qualify. Kubernetes uses slice
// components, so require the slice suffix as well.
func runtimeComponent(parts []string) int {
	for i, part := range parts {
		switch {
		case systemdContainerUnit(part, "docker-"):
			return i
		case systemdContainerUnit(part, "libpod-"):
			return i
		case part == "docker" || part == "libpod":
			return i
		case part == "kubepods" || part == "kubepods.slice":
			return i
		case strings.HasPrefix(part, "kubepods-") && strings.HasSuffix(part, ".slice"):
			return i
		}
	}
	return -1
}

func systemdContainerUnit(part, prefix string) bool {
	if !strings.HasPrefix(part, prefix) || !strings.HasSuffix(part, ".scope") {
		return false
	}
	id := strings.TrimSuffix(strings.TrimPrefix(part, prefix), ".scope")
	return len(id) == 64 && strings.Trim(id, "0123456789abcdef") == ""
}

// userScopeRuntime reports whether the runtime component sits under a login
// session or user manager. Rootless container runtimes use that subtree, so
// the caller must confirm the runtime with the process's mount namespace. A
// user.slice nested inside an LXC payload is part of the container's own
// hierarchy and does not count.
func userScopeRuntime(cgroup string) bool {
	for _, line := range strings.Split(cgroup, "\n") {
		i := strings.Index(line, "::")
		if i < 0 {
			continue
		}
		parts := strings.Split(strings.Trim(line[i+2:], "/"), "/")
		if lxcName(parts) != "" {
			return false
		}
		idx := runtimeComponent(parts)
		if idx < 0 {
			continue
		}
		for _, part := range parts[:idx] {
			if part == "user.slice" || strings.HasPrefix(part, "user-") {
				return true
			}
		}
		return false
	}
	return false
}

func processStartMatches(base, startTime string) bool {
	want, err := time.Parse(time.RFC3339Nano, startTime)
	if err != nil {
		return false
	}
	stat, err := os.ReadFile(base + "/stat")
	if err != nil {
		return false
	}
	// The comm field can contain spaces, so parse after the last ')'.
	end := strings.LastIndexByte(string(stat), ')')
	if end < 0 {
		return false
	}
	fields := strings.Fields(string(stat)[end+1:])
	// fields[0] is state (field 3); starttime is field 22.
	if len(fields) < 20 {
		return false
	}
	ticks, err := strconv.ParseUint(fields[19], 10, 64)
	if err != nil {
		return false
	}
	btime, err := bootTime()
	if err != nil {
		return false
	}
	got := time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
	diff := got.Sub(want)
	return diff > -2*time.Second && diff < 2*time.Second
}

func bootTime() (int64, error) {
	data, err := os.ReadFile(procRoot + "/stat")
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "btime ") {
			return strconv.ParseInt(strings.TrimSpace(strings.TrimPrefix(line, "btime ")), 10, 64)
		}
	}
	return 0, fmt.Errorf("btime not found in %s/stat", procRoot)
}
