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
	// Source names how the verdict was reached: namespace, cgroup, uid or
	// fallback (the parent decided it). Only the metric reads it.
	Source string
}

// resolveProcessLayer returns the container layer ("container" or
// "unverified") and, for a named LXC/Incus payload, its name. A user-scope
// runtime is "unverified": it may be a real rootless container, but a user can
// create the same cgroup name, so it must not inherit host or container
// suppression.
//
// The event-carried namespace snapshot wins over /proc: it was captured at
// exec time, so it answers for a process that has already exited. /proc only
// refines the verdict with a container name and separates a runtime container
// from a bare user namespace.
func resolveProcessLayer(pid uint32, startTime string, uid uint32, ns *eventNamespaces) processLayer {
	if ns != nil && ns.Pid.Inum != 0 {
		switch {
		case ns.Pid.IsHost:
			// A container can run in the host pid namespace (--pid=host, or
			// Incus keep=pid). The cgroup is the only thing that tells them
			// apart, and the cost is one /proc read on a live process.
			if r := cgroupLayer(pid, startTime); r.Resolved && r.Layer != "" {
				r.Source = "namespace+cgroup"
				return r
			}
			return processLayer{Resolved: true, Source: "namespace"}
		case ns.Mnt.Inum != 0 && ns.Mnt.IsHost:
			// A foreign pid namespace with the host mount namespace: the
			// process can write host paths, so it has host blast radius.
			return processLayer{Resolved: true, Source: "namespace"}
		}
		r := containerizedLayer(pid, startTime, uid)
		// Keep the refinement that actually answered: namespace says
		// "container", the cgroup or the uid says which kind.
		r.Source = "namespace+" + r.Source
		return r
	}
	return cgroupLayer(pid, startTime)
}

// cgroupLayer is the pre-namespace path and the refinement for a container
// verdict: the live cgroup, guarded against pid reuse.
func cgroupLayer(pid uint32, startTime string) processLayer {
	cgroup, ok := cgroupPath(pid, startTime)
	if !ok {
		return processLayer{}
	}
	name, inContainer := containerNameFromCgroup(cgroup)
	if !inContainer {
		return processLayer{Resolved: true, Source: "cgroup"}
	}
	if userScopeRuntime(cgroup) {
		return processLayer{Layer: "unverified", Resolved: true, Source: "cgroup"}
	}
	return processLayer{Name: name, Layer: "container", Resolved: true, Source: "cgroup"}
}

// containerizedLayer decides what kind of container a namespace says the
// process is in, using the cgroup for the name and to reject a user-created
// sandbox, and the host-visible uid when the cgroup is gone.
func containerizedLayer(pid uint32, startTime string, uid uint32) processLayer {
	if r := cgroupLayer(pid, startTime); r.Resolved {
		if r.Layer == "container" {
			return r
		}
		// A pid namespace with no runtime cgroup is something a user can
		// create with unshare. Do not let it inherit host or container
		// suppression.
		return processLayer{Layer: "unverified", Resolved: true, Source: "cgroup"}
	}
	if uid >= incusIDMapBase {
		return processLayer{Layer: "container", Resolved: true, Source: "uid"}
	}
	return processLayer{Layer: "unverified", Resolved: true, Source: "uid"}
}

// containerForProcessLayer keeps the two-value shape for callers that only
// need the container verdict; resolveProcessLayer carries the host/unknown
// distinction.
func containerForProcessLayer(pid uint32, startTime string) (string, string) {
	r := resolveProcessLayer(pid, startTime, 0, nil)
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
	if r := resolveProcessLayer(body.Process.PID, body.Process.StartTime, body.Process.UID, body.Process.NS); r.Resolved {
		return r
	}
	if p := body.Parent; p != nil {
		if r := resolveProcessLayer(p.PID, p.StartTime, p.UID, p.NS); r.Resolved {
			r.Source = "fallback"
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
		return processLayer{Layer: "container", Source: "uid"}
	}
	if p := body.Parent; p != nil && p.UID >= incusIDMapBase {
		return processLayer{Layer: "container", Source: "uid"}
	}
	// Both processes are gone and neither is idmapped. A dead login-uid
	// process can be a rootless container whose cgroup is no longer in /proc,
	// so it must not inherit host automation or host-only filters. System
	// accounts stay host: package helpers run as `_apt` (42) or `pcp` (996),
	// they exit before the line is read, and no rootless runtime starts from a
	// system uid.
	if body.Process.UID >= loginUIDBase {
		return processLayer{Layer: "unverified", Source: "uid"}
	}
	if p := body.Parent; p != nil && p.UID >= loginUIDBase {
		return processLayer{Layer: "unverified", Source: "uid"}
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
	got, ok := readProcessStart(base)
	if !ok {
		return false
	}
	return startTimeWithin(got, startTime)
}

// readProcessStart renders /proc/<pid>/stat field 22 as a wall-clock time,
// which is what Tetragon reports as start_time.
func readProcessStart(base string) (string, bool) {
	stat, err := os.ReadFile(base + "/stat")
	if err != nil {
		return "", false
	}
	// The comm field can contain spaces, so parse after the last ')'.
	end := strings.LastIndexByte(string(stat), ')')
	if end < 0 {
		return "", false
	}
	fields := strings.Fields(string(stat)[end+1:])
	// fields[0] is state (field 3); starttime is field 22.
	if len(fields) < 20 {
		return "", false
	}
	ticks, err := strconv.ParseUint(fields[19], 10, 64)
	if err != nil {
		return "", false
	}
	btime, err := bootTime()
	if err != nil {
		return "", false
	}
	got := time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
	return got.UTC().Format(time.RFC3339Nano), true
}

// startTimeWithin is the tolerance Tetragon's own timestamps need: the event
// and /proc agree to well under a second, so two seconds is slack, not a
// loophole for a reused pid.
func startTimeWithin(got, want string) bool {
	g, err := time.Parse(time.RFC3339Nano, got)
	if err != nil {
		return false
	}
	w, err := time.Parse(time.RFC3339Nano, want)
	if err != nil {
		return false
	}
	diff := g.Sub(w)
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
