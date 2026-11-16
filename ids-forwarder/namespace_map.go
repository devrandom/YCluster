package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
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
// publishes pid-namespace inum -> the cgroup texts its processes carried. One
// container spans many texts (a scope per service), and a namespace can in
// principle be shared by two containers, so this side keeps every parsing rule
// and decides whether the texts agree: the file is only a lookup table.
const (
	// namespaceMapTTL bounds how often the published file is re-read.
	namespaceMapTTL = 10 * time.Second
	// namespaceMapMaxAge stops a publisher that died from leaving stale
	// namespaces behind: an old map reads as empty, which falls back to the
	// uid heuristic rather than attributing an event to a container that may
	// no longer own that inum.
	namespaceMapMaxAge = 5 * time.Minute
	// namespaceMapMaxBytes bounds what the forwarder will parse. The publisher
	// already caps its own output; this is the reader's own limit, because the
	// file is written from state a tenant can grow.
	namespaceMapMaxBytes = 1 << 20
)

var (
	namespaceMapPath string
	namespaceMapMu   sync.Mutex
	namespaceMapAt   time.Time
	namespaceMapData map[uint32][]string
	// namespaceMapProblem is the last problem reported, so a condition that
	// lasts across re-reads is logged once rather than every 10 seconds.
	namespaceMapProblem   string
	namespaceMapProblemMu sync.Mutex
)

// noteNamespaceMapProblem reports a map problem when it is new. The file is
// re-read every namespaceMapTTL, which would otherwise turn a skewed rollout
// into thousands of journal lines a day.
func noteNamespaceMapProblem(format string, args ...any) {
	message := fmt.Sprintf(format, args...)
	namespaceMapProblemMu.Lock()
	changed := message != namespaceMapProblem
	namespaceMapProblem = message
	namespaceMapProblemMu.Unlock()
	if changed {
		log.Printf("%s", message)
	}
}

// clearNamespaceMapProblem re-arms the log line for the next time it happens.
func clearNamespaceMapProblem() {
	namespaceMapProblemMu.Lock()
	namespaceMapProblem = ""
	namespaceMapProblemMu.Unlock()
}

// namespaceMapLookup returns the cgroup texts published for a pid namespace.
// inum 0 is a replayed pre-flag line and never matches.
func namespaceMapLookup(inum uint32) ([]string, bool) {
	if namespaceMapPath == "" || inum == 0 {
		return nil, false
	}
	namespaceMapMu.Lock()
	defer namespaceMapMu.Unlock()
	if time.Since(namespaceMapAt) > namespaceMapTTL {
		namespaceMapAt = time.Now()
		namespaceMapData = readNamespaceMap(namespaceMapPath)
	}
	texts, ok := namespaceMapData[inum]
	return texts, ok
}

// readNamespaceMap parses the published file. Every failure is an empty map:
// attribution must degrade to the uid heuristic, never to a guess.
func readNamespaceMap(path string) map[uint32][]string {
	info, err := os.Stat(path)
	if err != nil || time.Since(info.ModTime()) > namespaceMapMaxAge {
		return nil
	}
	if info.Size() > namespaceMapMaxBytes {
		noteNamespaceMapProblem("ids-forwarder: namespace map %s is %d bytes, over the %d-byte limit; ignoring it",
			path, info.Size(), namespaceMapMaxBytes)
		return nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	// The size gate above is a cheap check; the limit is what actually bounds
	// the allocation, because the file can be replaced between the two.
	data, err := io.ReadAll(io.LimitReader(f, namespaceMapMaxBytes+1))
	if err != nil {
		return nil
	}
	if len(data) > namespaceMapMaxBytes {
		noteNamespaceMapProblem("ids-forwarder: namespace map %s grew past the %d-byte limit to %d bytes; ignoring it",
			path, namespaceMapMaxBytes, len(data))
		return nil
	}
	var raw map[string][]string
	if err := json.Unmarshal(data, &raw); err != nil {
		// Not silent: a mismatched publisher and forwarder look identical to
		// a host without containers otherwise.
		noteNamespaceMapProblem("ids-forwarder: namespace map %s unreadable: %v", path, err)
		return nil
	}
	clearNamespaceMapProblem()
	entries := make(map[uint32][]string, len(raw))
	for key, texts := range raw {
		inum, err := strconv.ParseUint(key, 10, 32)
		if err != nil {
			continue
		}
		kept := make([]string, 0, len(texts))
		for _, text := range texts {
			if text != "" {
				kept = append(kept, text)
			}
		}
		if len(kept) == 0 {
			continue
		}
		entries[uint32(inum)] = kept
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

// namespaceMapVerdict names the container the published map saw in a pid
// namespace. One container spans one cgroup per service it runs, so several
// texts are normal and they all have to name the same container; two
// containers sharing a namespace is the case this refuses to name, because
// either name would be wrong for half of the events. A refusal is not a
// verdict: the caller keeps its uid heuristic, which can still tell that an
// idmapped process is in some container, just not which.
func namespaceMapVerdict(inum uint32) (processLayer, bool) {
	texts, ok := namespaceMapLookup(inum)
	if !ok || len(texts) == 0 {
		return processLayer{}, false
	}
	verdict := cgroupVerdict(texts[0])
	if verdict.Layer != "container" {
		return processLayer{}, false
	}
	for _, text := range texts[1:] {
		other := cgroupVerdict(text)
		if other.Layer != "container" || other.Name != verdict.Name {
			return processLayer{}, false
		}
	}
	verdict.Source = "nsmap"
	return verdict, true
}
