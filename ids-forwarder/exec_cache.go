package main

import "sync"

// execEntry is what the forwarder learned about one exec from the event
// stream itself. Tetragon renders the exec id on every event and names the
// parent exec, so a later event can ask "who started this" without a /proc
// read and without the invoker still running.
type execEntry struct {
	uid uint32
	ns  *eventNamespaces
	bin string
	// hasParent is set only when the event carried the parent block, which
	// is what makes parentUID a verdict rather than a zero value.
	hasParent bool
	parentUID uint32
	parentID  string
}

// execCacheCap bounds the cache. At a few thousand execs per hour per host a
// full generation still covers the latency between an exec and the Sigma
// detection that quotes it.
const execCacheCap = 4096

var (
	execCacheMu   sync.Mutex
	execCacheNow  map[string]execEntry
	execCachePrev map[string]execEntry
)

// rememberExec records the event's process and its parent by exec id. The
// parent is stored separately: its own events may have been suppressed before
// the decision, and its identity is what the escalation and layer fallbacks
// need after /proc has forgotten it.
func rememberExec(body *tetragonLog) {
	if body == nil {
		return
	}
	execCacheMu.Lock()
	defer execCacheMu.Unlock()
	if body.Process.ExecID != "" {
		entry := execEntry{
			uid:      body.Process.UID,
			ns:       body.Process.NS,
			bin:      body.Process.Binary,
			parentID: body.Process.ParentExecID,
		}
		if parent := body.Parent; parent != nil && parent.PID != 0 {
			entry.hasParent = true
			entry.parentUID = parent.UID
			if parent.ExecID != "" {
				entry.parentID = parent.ExecID
			}
		}
		execCacheStore(body.Process.ExecID, entry)
	}
	if parent := body.Parent; parent != nil && parent.ExecID != "" {
		execCacheStore(parent.ExecID, execEntry{
			uid:      parent.UID,
			ns:       parent.NS,
			bin:      parent.Binary,
			parentID: parent.ParentExecID,
		})
	}
}

// execCacheStore keeps two generations instead of clearing on overflow: the
// generation being replaced stays readable, so a burst of execs cannot evict
// the parent entry a detection in flight is about to ask for.
func execCacheStore(id string, entry execEntry) {
	if execCacheNow == nil {
		execCacheNow = map[string]execEntry{}
	}
	if _, exists := execCacheNow[id]; !exists && len(execCacheNow) >= execCacheCap {
		execCachePrev = execCacheNow
		execCacheNow = map[string]execEntry{}
	}
	execCacheNow[id] = entry
}

// execLookup finds an exec recorded from the event stream. An empty id is
// never a hit: it is what a replayed pre-flag line carries.
func execLookup(id string) (execEntry, bool) {
	if id == "" {
		return execEntry{}, false
	}
	execCacheMu.Lock()
	defer execCacheMu.Unlock()
	if entry, ok := execCacheNow[id]; ok {
		return entry, true
	}
	entry, ok := execCachePrev[id]
	return entry, ok
}

// resetExecCache clears both generations. Tests use it between cases.
func resetExecCache() {
	execCacheMu.Lock()
	execCacheNow = nil
	execCachePrev = nil
	execCacheMu.Unlock()
}
