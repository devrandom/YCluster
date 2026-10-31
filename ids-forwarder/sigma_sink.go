package main

import (
	"encoding/json"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"ycluster.local/ids-forwarder/sigma"
)

// sigmaSocketQueue bounds the best-effort copy of normalized events sent to
// the RSigma rule engine. The socket is an additional consumer: when RSigma
// is down or slow, events are dropped here and the alert path is unaffected.
const sigmaSocketQueue = 8192

// sigmaSink writes normalized Tetragon events to the RSigma unix socket.
// Offer never blocks: a full queue or a missing engine only increments
// counters, so the Alertmanager path cannot be held up by the rule layer.
type sigmaSink struct {
	path string
	logf func(format string, args ...any)
	ch   chan []byte
	stop chan struct{}
	done chan struct{}

	sent       atomic.Int64
	errors     atomic.Int64
	dropped    atomic.Int64
	dropLogged atomic.Bool
	dialLogged atomic.Bool

	closeOnce sync.Once
}

func newSigmaSink(path string, queue int, logf func(string, ...any)) *sigmaSink {
	if queue < 1 {
		queue = 1
	}
	if logf == nil {
		logf = func(string, ...any) {}
	}
	s := &sigmaSink{
		path: path,
		logf: logf,
		ch:   make(chan []byte, queue),
		stop: make(chan struct{}),
		done: make(chan struct{}),
	}
	go s.loop()
	return s
}

// offer normalizes one Tetragon line and queues it without blocking.
// Malformed lines are counted but never propagated: rule-layer failures
// must not turn into coverage gaps for the existing detections.
func (s *sigmaSink) offer(line string) {
	ev, ok, err := sigma.NormalizeLine(line)
	if err != nil {
		s.errors.Add(1)
		return
	}
	if !ok {
		return
	}
	b, err := json.Marshal(ev)
	if err != nil {
		s.errors.Add(1)
		return
	}
	select {
	case s.ch <- b:
	default:
		s.dropped.Add(1)
		if s.dropLogged.CompareAndSwap(false, true) {
			s.logf("sigma: queue full, dropping events for the rule engine")
		}
	}
}

// loop owns the connection. A failed dial keeps the pending event queued
// and retries with backoff, so a short RSigma restart does not need to be
// covered by the queue capacity alone.
func (s *sigmaSink) loop() {
	defer close(s.done)
	var (
		conn    net.Conn
		pending []byte
		backoff = time.Second
	)
	closeConn := func() {
		if conn != nil {
			_ = conn.Close()
			conn = nil
		}
	}
	defer closeConn()

	for {
		if pending == nil {
			select {
			case <-s.stop:
				return
			case pending = <-s.ch:
			}
		}
		if conn == nil {
			var ok bool
			conn, backoff, ok = s.connect(backoff)
			if !ok {
				return
			}
			continue
		}
		if _, err := conn.Write(append(pending, '\n')); err != nil {
			s.errors.Add(1)
			closeConn()
			continue
		}
		pending = nil
		s.sent.Add(1)
	}
}

// connect dials the rule engine, retrying with backoff until it succeeds or
// the sink is closed. ok=false means the sink stopped while backing off.
func (s *sigmaSink) connect(backoff time.Duration) (net.Conn, time.Duration, bool) {
	for {
		conn, err := net.DialTimeout("unix", s.path, 2*time.Second)
		if err == nil {
			s.dialLogged.Store(false)
			s.logf("sigma: connected to %s", s.path)
			return conn, time.Second, true
		}
		s.errors.Add(1)
		if s.dialLogged.CompareAndSwap(false, true) {
			s.logf("sigma: connect %s: %v", s.path, err)
		}
		if !sleepOrStop(s.stop, backoff) {
			return nil, backoff, false
		}
		if backoff < 30*time.Second {
			backoff *= 2
		}
	}
}

func (s *sigmaSink) close() {
	s.closeOnce.Do(func() {
		close(s.stop)
		<-s.done
	})
}

func (s *sigmaSink) depth() int64 {
	return int64(len(s.ch))
}

func sleepOrStop(stop <-chan struct{}, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-stop:
		return false
	case <-t.C:
		return true
	}
}
