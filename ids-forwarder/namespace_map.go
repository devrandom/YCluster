package main

import (
	"encoding/json"
	"os"
	"strconv"
	"sync"
	"time"
)

// The kernel exposes a namespace's owner only through the processes living in
// it: /proc/<pid>/ns/pid is a symlink an unprivileged reader cannot follow
// (ptrace-level access), while /proc/<pid>/cgroup is world-readable. The
// forwarder needs the first to attribute an event whose process has exited and
// the second to name the container, so a root-side timer pairs them and
// publishes pid-namespace inum -> cgroup text here. This side keeps every
// parsing rule: the file is only a lookup table.
const (
	// namespaceMapTTL bounds how often the published file is re-read.
	namespaceMapTTL = 10 * time.Second
	// namespaceMapMaxAge stops a publisher that died from leaving stale
	// namespaces behind: an old map reads as empty, which falls back to the
	// uid heuristic rather than attributing an event to a container that may
	// no longer own that inum.
	namespaceMapMaxAge = 5 * time.Minute
)

var (
	namespaceMapPath string
	namespaceMapMu   sync.Mutex
	namespaceMapAt   time.Time
	namespaceMapData map[uint32]string
)

// namespaceMapLookup returns the cgroup text of a process that was in the
// given pid namespace. inum 0 is a replayed pre-flag line and never matches.
func namespaceMapLookup(inum uint32) (string, bool) {
	if namespaceMapPath == "" || inum == 0 {
		return "", false
	}
	namespaceMapMu.Lock()
	defer namespaceMapMu.Unlock()
	if time.Since(namespaceMapAt) > namespaceMapTTL {
		namespaceMapAt = time.Now()
		namespaceMapData = readNamespaceMap(namespaceMapPath)
	}
	cgroup, ok := namespaceMapData[inum]
	return cgroup, ok
}

// readNamespaceMap parses the published file. Every failure is an empty map:
// attribution must degrade to the uid heuristic, never to a guess.
func readNamespaceMap(path string) map[uint32]string {
	info, err := os.Stat(path)
	if err != nil || time.Since(info.ModTime()) > namespaceMapMaxAge {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var raw map[string]string
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil
	}
	entries := make(map[uint32]string, len(raw))
	for key, cgroup := range raw {
		inum, err := strconv.ParseUint(key, 10, 32)
		if err != nil || cgroup == "" {
			continue
		}
		entries[uint32(inum)] = cgroup
	}
	return entries
}

// resetNamespaceMap clears the cache. Tests use it between cases.
func resetNamespaceMap() {
	namespaceMapMu.Lock()
	namespaceMapAt = time.Time{}
	namespaceMapData = nil
	namespaceMapMu.Unlock()
}
