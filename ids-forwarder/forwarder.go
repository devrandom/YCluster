package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	sourceTetragon = "tetragon"
	sourceCrowdSec = "crowdsec"
	alertName      = "IDSAlert"
)

type config struct {
	alertmanagerURL string
	tetragonLog     string
	events          string
	execBinaries    string
	httpListen      string
	metricsListen   string
	hostname        string
	queueMax        int
	dedupWindow     time.Duration
	alertTTL        time.Duration
	maxEventAge     time.Duration
	startupGrace    time.Duration
	cursorFile      string
	spoolFile       string
	spoolMaxBytes   int64
	detectionsFile  string
	detectionsBytes int64
	detectionsKeep  int
	sigmaSocket     string
	sigmaDetections string
	sigmaCursorFile string
	postTimeout     time.Duration
	pollInterval    time.Duration
	logf            func(format string, args ...any)
}

func defaultConfig() config {
	return config{
		alertmanagerURL: "http://127.0.0.1:9093",
		tetragonLog:     "/var/log/tetragon/tetragon.log",
		events:          "process_kprobe",
		execBinaries:    "bash,sh,dash,zsh,fish,sudo,su,apt,apt-get,dpkg,snap,curl,wget",
		httpListen:      "",
		metricsListen:   "127.0.0.1:2113",
		queueMax:        1000,
		dedupWindow:     5 * time.Minute,
		alertTTL:        10 * time.Minute,
		maxEventAge:     24 * time.Hour,
		startupGrace:    time.Minute,
		spoolMaxBytes:   100 << 20,
		detectionsFile:  "/var/lib/ids-forwarder/detections.jsonl",
		detectionsBytes: 50 << 20,
		detectionsKeep:  10,
		postTimeout:     5 * time.Second,
		pollInterval:    500 * time.Millisecond,
		logf:            log.Printf,
	}
}

type forwarder struct {
	cfg       config
	queue     alertQueue
	dedup     dedup
	client    *http.Client
	stats     metrics
	stop      chan struct{}
	once      sync.Once
	spool     *alertSpool
	cursor    *replayCursor
	tail      atomic.Pointer[tailer]
	sigmaTail *tailer
	sigma     *sigmaSink
	record    *detectionLog

	// wg tracks background goroutines (tailer, post loop) so tests can
	// coordinate shutdown.
	wg sync.WaitGroup

	receiverListen string
	metricsBind    string
	receiverSrv    *http.Server
	metricsSrv     *http.Server

	gapMu      sync.Mutex
	gapPending bool

	errMu sync.Mutex
	fatal error
}

func newForwarder(cfg config) (*forwarder, error) {
	if cfg.queueMax < 1 {
		return nil, errors.New("queue-max must be >= 1")
	}
	if cfg.dedupWindow < 0 {
		return nil, errors.New("dedup-window must be >= 0")
	}
	if cfg.spoolMaxBytes < 0 {
		return nil, errors.New("spool-max-bytes must be >= 0")
	}
	if cfg.cursorFile != "" && cfg.spoolFile == "" {
		return nil, errors.New("cursor-file requires spool-file: an in-memory queue can drop an alert while the cursor advances past it")
	}
	if cfg.logf == nil {
		cfg.logf = log.Printf
	}
	f := &forwarder{
		cfg:    cfg,
		queue:  alertQueue{max: cfg.queueMax},
		dedup:  newDedup(cfg.dedupWindow),
		client: &http.Client{Timeout: cfg.postTimeout},
		stop:   make(chan struct{}),
	}
	if cfg.spoolFile != "" {
		spool, err := openAlertSpool(cfg.spoolFile, cfg.spoolMaxBytes, cfg.logf)
		if err != nil {
			return nil, fmt.Errorf("open alert spool: %w", err)
		}
		f.spool = spool
	}
	if cfg.cursorFile != "" {
		f.cursor = newReplayCursor(cfg.cursorFile, cfg.logf)
		f.cursor.onError = func(error) { f.stats.cursorErrors.Add(1) }
		f.cursor.load()
	}
	if cfg.detectionsFile != "" {
		record, err := openDetectionLog(cfg.detectionsFile, cfg.detectionsBytes, cfg.detectionsKeep)
		if err != nil {
			return nil, fmt.Errorf("open detections file: %w", err)
		}
		f.record = record
	}
	if cfg.sigmaSocket != "" {
		f.sigma = newSigmaSink(cfg.sigmaSocket, sigmaSocketQueue, cfg.logf)
	}
	return f, nil
}

// shutdown signals all loops to exit exactly once.
func (f *forwarder) shutdown() {
	f.once.Do(func() {
		close(f.stop)
		if f.spool != nil {
			f.spool.stop()
		}
		if tl := f.tail.Load(); tl != nil {
			tl.close()
		}
		if f.sigmaTail != nil {
			f.sigmaTail.close()
		}
		if f.sigma != nil {
			f.sigma.close()
		}
		if f.receiverSrv != nil {
			_ = f.receiverSrv.Close()
		}
		if f.metricsSrv != nil {
			_ = f.metricsSrv.Close()
		}
	})
}

// fail records a fatal error and starts shutdown. Callers must not hold
// the tailer lock: shutdown closes the tailer.
func (f *forwarder) fail(err error) {
	if err == nil {
		return
	}
	f.errMu.Lock()
	if f.fatal == nil {
		f.fatal = err
	}
	f.errMu.Unlock()
	f.shutdown()
}

// failure returns the first fatal error, if any. A non-nil result makes
// systemd's Restart=on-failure restart the process instead of treating a
// lost spool as a clean stop.
func (f *forwarder) failure() error {
	f.errMu.Lock()
	defer f.errMu.Unlock()
	return f.fatal
}

func (f *forwarder) closeState() {
	if f.cursor != nil {
		f.cursor.flush()
	}
	if f.spool != nil {
		f.spool.close()
	}
	if err := f.record.close(); err != nil {
		f.cfg.logf("close detections file: %v", err)
	}
}

// runWith starts every configured component and blocks until ctx is
// cancelled or shutdown is requested. This is the production entry point
// after flag parsing.
func runWith(ctx context.Context, cfg config) error {
	f, err := newForwarder(cfg)
	if err != nil {
		return err
	}
	if err := f.start(); err != nil {
		f.shutdown()
		f.wg.Wait()
		f.closeState()
		return err
	}
	// Watch for cancellation only after every component has published its
	// listener and spool state; shutdown reads them.
	go func() {
		select {
		case <-ctx.Done():
			f.shutdown()
		case <-f.stop:
		}
	}()
	select {
	case <-ctx.Done():
	case <-f.stop:
	}
	f.shutdown()
	f.wg.Wait()
	f.closeState()
	return f.failure()
}

// start launches the post loop, replay/tailer, receiver and metrics. The
// post loop starts first so a spool left by a previous process can drain
// while the new process replays the sensor log.
func (f *forwarder) start() error {
	f.wg.Add(1)
	go f.postLoop()
	if f.cursor != nil && f.cursor.path != "" {
		f.wg.Add(1)
		go f.cursorLoop()
	}
	if f.stopping() {
		return nil
	}
	if f.cfg.tetragonLog != "" {
		if err := f.startTailer(); err != nil {
			return err
		}
	}
	if f.stopping() {
		return nil
	}
	if f.cfg.httpListen != "" {
		if err := f.startReceiver(); err != nil {
			return err
		}
	}
	if f.stopping() {
		return nil
	}
	if f.cfg.sigmaDetections != "" {
		// The Sigma path is additive: a bad detection sink must not take
		// down the hand-written rules or the Alertmanager spool.
		if err := f.startSigmaDetections(); err != nil {
			f.stats.sigmaTailErrors.Add(1)
			f.cfg.logf("sigma detections disabled: %v", err)
		}
	}
	if f.stopping() {
		return nil
	}
	if f.cfg.metricsListen != "" {
		if err := f.startMetrics(); err != nil {
			return err
		}
	}
	return nil
}

func (f *forwarder) stopping() bool {
	select {
	case <-f.stop:
		return true
	default:
		return false
	}
}

func (f *forwarder) cursorLoop() {
	defer f.wg.Done()
	t := time.NewTicker(f.cursor.saveInterval)
	defer t.Stop()
	for {
		select {
		case <-f.stop:
			return
		case <-t.C:
			f.cursor.flush()
		}
	}
}

// receiverAddr returns the address the CrowdSec receiver is listening on,
// or "" when the receiver is disabled. Used by tests.
func (f *forwarder) receiverAddr() string { return f.receiverListen }

// metricsAddr returns the address the metrics server is listening on, or
// "" when metrics are disabled. Used by tests.
func (f *forwarder) metricsAddr() string { return f.metricsBind }

// startTailer replays any retained events after the durable cursor, then
// tails the live file. On rotation the tailer invokes replay again so
// intermediate rotated files cannot be skipped while the process is busy.
func (f *forwarder) startTailer() error {
	// Hold the current inode open before replaying. If Tetragon rotates
	// during the replay scan, this descriptor still carries any lines
	// appended to the old file, so the handoff cannot miss them.
	current, err := os.Open(f.cfg.tetragonLog)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("open %s: %w", f.cfg.tetragonLog, err)
	}
	startOffset := openFileSize(current)
	if f.replayWanted() {
		offset, err := f.replayTetragonLogs(current)
		if err != nil {
			closeFile(current)
			return fmt.Errorf("replay tetragon logs: %w", err)
		}
		startOffset = offset
	}
	if f.stopping() {
		closeFile(current)
		return nil
	}
	tl, err := f.buildTailer(current, startOffset)
	if err != nil {
		return err
	}
	f.attachTailer(tl)
	return nil
}

// replayWanted reports whether retained files should be scanned before
// tailing. Without a cursor the forwarder tails live events only.
func (f *forwarder) replayWanted() bool {
	return f.cursor != nil && (f.cursor.hasState() || f.cfg.startupGrace > 0)
}

func openFileSize(f *os.File) int64 {
	if f == nil {
		return 0
	}
	if info, err := f.Stat(); err == nil {
		return info.Size()
	}
	return 0
}

func closeFile(f *os.File) {
	if f != nil {
		f.Close()
	}
}

func (f *forwarder) buildTailer(current *os.File, startOffset int64) (*tailer, error) {
	handler := f.tailHandler()
	if current != nil {
		tl, err := newTailerFromFile(current, f.cfg.tetragonLog, f.cfg.pollInterval, startOffset, handler)
		if err != nil {
			return nil, fmt.Errorf("tail %s: %w", f.cfg.tetragonLog, err)
		}
		return tl, nil
	}
	// The file may not exist until Tetragon first runs; retry in the
	// background and require only that the directory exists.
	return newRetryTailer(f.cfg.tetragonLog, f.cfg.pollInterval, handler), nil
}

func (f *forwarder) attachTailer(tl *tailer) {
	tl.onRotate = func() {
		if _, err := f.replayTetragonLogs(nil); err != nil {
			f.fail(err)
		}
	}
	f.tail.Store(tl)
	f.wg.Add(1)
	go func() {
		defer f.wg.Done()
		tl.run()
	}()
}

// tailHandler forwards one live line. A spool failure is fatal: the
// alert is not durable, so the process must restart and replay it.
func (f *forwarder) tailHandler() func([]byte) {
	return func(line []byte) {
		if f.stopping() {
			return
		}
		if err := f.handleLine(string(line)); err != nil {
			f.cfg.logf("fatal: %v", err)
			// The handler runs under the tailer lock and shutdown closes
			// the tailer, so report from a separate goroutine.
			go f.fail(err)
		}
	}
}

// handleLine durably queues one Tetragon line. The cursor advances only
// after the alert is in the spool (or the line needs no alert), so a
// crash or restart replays it rather than losing it.
func (f *forwarder) handleLine(line string) error {
	line = strings.TrimSpace(line)
	if line == "" {
		return nil
	}
	if f.sigma != nil {
		// Best-effort copy for the rule engine: never blocks and never
		// changes the alert decision below.
		f.sigma.offer(line)
	}
	a, meta, ok, suppressed := tetragonAlertDecision(line, f.cfg)
	if meta.time.IsZero() {
		return nil
	}
	if !ok {
		f.stats.suppress(suppressed)
		f.cursor.advance(meta.time, meta.hash)
		return nil
	}
	if err := f.enqueue(a); err != nil {
		return &spoolError{err: fmt.Errorf("queue alert %s: %w", a.Labels["rule_id"], err)}
	}
	f.cursor.advance(meta.time, meta.hash)
	return nil
}

// enqueue makes one alert durable. It returns an error only when the
// alert could not be stored; callers must then keep their source cursor
// where it is so the event is retried.
func (f *forwarder) enqueue(a alert) error {
	key := a.dedupKey()
	if f.dedup.seen(key) {
		f.stats.deduped.Add(1)
		return nil
	}
	if f.spool != nil {
		if err := f.spool.append(a); err != nil {
			if errors.Is(err, errSpoolEntryTooLarge) {
				// A single pathological event must not wedge replay.
				f.stats.oversized.Add(1)
				f.stats.dropped.Add(1)
				f.cfg.logf("drop oversized alert %s: %v", a.Labels["rule_id"], err)
				return nil
			}
			f.stats.dropped.Add(1)
			return err
		}
		// Mark only after the append is durable: a failed append must stay
		// retryable instead of being suppressed as a duplicate.
		f.dedup.mark(key)
		f.recordDetection(a)
		return nil
	}
	if !f.queue.push(a) {
		f.stats.dropped.Add(1)
		f.cfg.logf("queue full, dropping alert")
		return nil
	}
	f.dedup.mark(key)
	f.recordDetection(a)
	return nil
}

// recordDetection appends the detection to the local record file. This is
// the durable history of what the sensor classified as a detection, kept
// separately from Alertmanager, which is only the paging path. A write
// failure is counted and logged rather than returned: a full disk must not
// stall the cursor or stop coverage alerts from being delivered.
func (f *forwarder) recordDetection(a alert) {
	if f.record == nil {
		return
	}
	if err := f.record.append(newDetectionRecord(a)); err != nil {
		f.stats.recordErrors.Add(1)
		f.cfg.logf("record detection %s: %v", a.Labels["rule_id"], err)
		return
	}
	f.stats.recorded.Add(1)
}
