package main

import (
	"sync"
	"sync/atomic"
)

// Load reports in-flight request counts per backend URL. Read-only view
// used by the router to pick the least-loaded healthy backend.
type Load interface {
	Count(urlStr string) int64
}

// LoadTracker is the read/write view used by the request handler to
// increment a backend's counter on dispatch and decrement it on
// completion.
type LoadTracker interface {
	Load
	Inc(urlStr string)
	Dec(urlStr string)
}

// LoadCounter is a lock-free in-flight counter keyed by backend URL
// string. Zero value is ready to use.
type LoadCounter struct {
	counts sync.Map // map[string]*atomic.Int64
}

func NewLoadCounter() *LoadCounter { return &LoadCounter{} }

func (lc *LoadCounter) get(urlStr string) *atomic.Int64 {
	if v, ok := lc.counts.Load(urlStr); ok {
		return v.(*atomic.Int64)
	}
	v, _ := lc.counts.LoadOrStore(urlStr, new(atomic.Int64))
	return v.(*atomic.Int64)
}

func (lc *LoadCounter) Count(urlStr string) int64 { return lc.get(urlStr).Load() }
func (lc *LoadCounter) Inc(urlStr string)         { lc.get(urlStr).Add(1) }
func (lc *LoadCounter) Dec(urlStr string)         { lc.get(urlStr).Add(-1) }
