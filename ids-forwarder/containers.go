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
	if pid == 0 || startTime == "" {
		return "", false
	}
	base := fmt.Sprintf("%s/%d", procRoot, pid)
	cgroup, err := os.ReadFile(base + "/cgroup")
	if err != nil {
		return "", false
	}
	name, ok := containerNameFromCgroup(string(cgroup))
	if !ok {
		return "", false
	}
	if !processStartMatches(base, startTime) {
		return "", false
	}
	return name, true
}

// containerForEvent attributes an event to a container. Short-lived children
// (motd scripts, shell probes) are often gone by the time the line is read, so
// when the process itself no longer resolves, fall back to its parent, which
// is normally the long-lived process the container started.
func containerForEvent(body *tetragonLog) (string, bool) {
	if body == nil {
		return "", false
	}
	if name, ok := containerForProcess(body.Process.PID, body.Process.StartTime); ok {
		return name, true
	}
	if p := body.Parent; p != nil {
		if name, ok := containerForProcess(p.PID, p.StartTime); ok {
			return name, true
		}
	}
	// Both processes can be gone by the time the line is read. Incus maps
	// container accounts into a host uid range that no host account uses
	// here: host accounts top out at 65534, containers start at 1,000,000.
	// The uid is in the event, so attribution does not depend on /proc.
	if body.Process.UID >= incusIDMapBase {
		return "", true
	}
	if p := body.Parent; p != nil && p.UID >= incusIDMapBase {
		return "", true
	}
	return "", false
}

// incusIDMapBase is the lowest host uid the Incus idmap can produce. It is
// below the cluster's 1,000,000 base so a different idmap range still
// matches, and above every host account (nobody is 65534).
const incusIDMapBase = 100000

// containerNameFromCgroup recognises the cgroup paths used by Incus/LXC,
// Docker/Podman and Kubernetes. Only LXC-style paths carry a name.
func containerNameFromCgroup(cgroup string) (string, bool) {
	for _, line := range strings.Split(cgroup, "\n") {
		i := strings.Index(line, "::")
		if i < 0 {
			continue
		}
		parts := strings.Split(line[i+2:], "/")
		for j, part := range parts {
			if strings.HasPrefix(part, "lxc.payload.") {
				return strings.TrimPrefix(part, "lxc.payload."), true
			}
			if part == "lxc" && j+1 < len(parts) {
				return parts[j+1], true
			}
		}
		path := line[i+2:]
		if strings.Contains(path, "docker-") ||
			strings.Contains(path, "libpod-") ||
			strings.Contains(path, "kubepods") {
			return "", true
		}
	}
	return "", false
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
