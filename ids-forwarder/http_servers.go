package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"
)

// startReceiver runs the optional localhost HTTP receiver used by the
// CrowdSec notification-http plugin.
func (f *forwarder) startReceiver() error {
	mux := http.NewServeMux()
	mux.HandleFunc("/crowdsec", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		var payload []crowdsecAlert
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&payload); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		for _, item := range payload {
			if err := f.enqueue(crowdsecToAlert(item, f.cfg)); err != nil {
				// The alert is not durable: tell CrowdSec to retry, then
				// restart so the failure is not hidden behind a 503. The
				// response is flushed first because shutdown closes the
				// listener from another goroutine.
				f.cfg.logf("fatal: crowdsec enqueue: %v", err)
				http.Error(w, "queue unavailable", http.StatusServiceUnavailable)
				if fl, ok := w.(http.Flusher); ok {
					fl.Flush()
				}
				go f.fail(err)
				return
			}
		}
		w.WriteHeader(http.StatusOK)
	})
	srv := &http.Server{Addr: f.cfg.httpListen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", f.cfg.httpListen)
	if err != nil {
		return err
	}
	if isNotLoopback(ln.Addr()) {
		ln.Close()
		return errors.New("http-listen must bind to a loopback address")
	}
	f.receiverListen = ln.Addr().String()
	f.receiverSrv = srv
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			f.cfg.logf("receiver: %v", err)
			os.Exit(1)
		}
	}()
	return nil
}

func isNotLoopback(addr net.Addr) bool {
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return true
	}
	ip := net.ParseIP(host)
	return ip == nil || !ip.IsLoopback()
}

func (f *forwarder) startMetrics() error {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		depth := f.queue.depth()
		pending := int64(0)
		if f.spool != nil {
			depth = f.spool.depth()
			pending = f.spool.pendingBytes()
		}
		fmt.Fprintf(w, "ids_forwarder_up 1\n")
		fmt.Fprintf(w, "ids_forwarder_queue_depth %d\n", depth)
		fmt.Fprintf(w, "ids_forwarder_spool_bytes %d\n", pending)
		fmt.Fprintf(w, "ids_forwarder_forwarded_total %d\n", f.stats.forwarded.Load())
		fmt.Fprintf(w, "ids_forwarder_dropped_total %d\n", f.stats.dropped.Load())
		fmt.Fprintf(w, "ids_forwarder_deduped_total %d\n", f.stats.deduped.Load())
		fmt.Fprintf(w, "ids_forwarder_coalesced_total %d\n", f.stats.coalesced.Load())
		fmt.Fprintf(w, "ids_forwarder_boot_suppressed_total %d\n", f.stats.bootSuppressed.Load())
		fmt.Fprintf(w, "ids_forwarder_rate_limited_total %d\n", f.stats.rateLimited.Load())
		fmt.Fprintf(w, "ids_forwarder_post_failures_total %d\n", f.stats.failures.Load())
		fmt.Fprintf(w, "ids_forwarder_alertmanager_partial_total %d\n", f.stats.partialDelivery.Load())
		fmt.Fprintf(w, "ids_forwarder_expired_total %d\n", f.stats.expired.Load())
		fmt.Fprintf(w, "ids_forwarder_gap_total %d\n", f.stats.gaps.Load())
		fmt.Fprintf(w, "ids_forwarder_oversized_total %d\n", f.stats.oversized.Load())
		fmt.Fprintf(w, "ids_forwarder_rejected_total %d\n", f.stats.rejected.Load())
		fmt.Fprintf(w, "ids_forwarder_cursor_errors_total %d\n", f.stats.cursorErrors.Load())
		fmt.Fprintf(w, "ids_forwarder_recorded_total %d\n", f.stats.recorded.Load())
		fmt.Fprintf(w, "ids_forwarder_record_errors_total %d\n", f.stats.recordErrors.Load())
		var sigmaSent, sigmaErrors, sigmaDropped, sigmaDepth int64
		if f.sigma != nil {
			sigmaSent = f.sigma.sent.Load()
			sigmaErrors = f.sigma.errors.Load()
			sigmaDropped = f.sigma.dropped.Load()
			sigmaDepth = f.sigma.depth()
		}
		fmt.Fprintf(w, "ids_forwarder_sigma_sent_total %d\n", sigmaSent)
		fmt.Fprintf(w, "ids_forwarder_sigma_errors_total %d\n", sigmaErrors)
		fmt.Fprintf(w, "ids_forwarder_sigma_dropped_total %d\n", sigmaDropped)
		fmt.Fprintf(w, "ids_forwarder_sigma_queue_depth %d\n", sigmaDepth)
		fmt.Fprintf(w, "ids_forwarder_sigma_bad_total %d\n", f.stats.sigmaBad.Load())
		fmt.Fprintf(w, "ids_forwarder_sigma_tail_errors_total %d\n", f.stats.sigmaTailErrors.Load())
		fmt.Fprintf(w, "# HELP ids_forwarder_suppressed_total Events suppressed by routine-exec classification.\n")
		fmt.Fprintf(w, "# TYPE ids_forwarder_suppressed_total counter\n")
		suppressed := f.stats.suppressedCounts()
		reasons := make([]string, 0, len(suppressed))
		for reason := range suppressed {
			reasons = append(reasons, reason)
		}
		sort.Strings(reasons)
		for _, reason := range reasons {
			fmt.Fprintf(w, "ids_forwarder_suppressed_total{reason=%q} %d\n", reason, suppressed[reason])
		}
		fmt.Fprintf(w, "# HELP ids_forwarder_detections_total Classified detections by process origin, before dedup, coalescing and rate limiting.\n")
		fmt.Fprintf(w, "# TYPE ids_forwarder_detections_total counter\n")
		origins := f.stats.originCounts()
		names := make([]string, 0, len(origins))
		for origin := range origins {
			names = append(names, origin)
		}
		sort.Strings(names)
		for _, origin := range names {
			fmt.Fprintf(w, "ids_forwarder_detections_total{origin=%q} %d\n", origin, origins[origin])
		}
		fmt.Fprintf(w, "# HELP ids_forwarder_evidence_only_total Detections recorded as post-root evidence and not delivered, by class.\n")
		fmt.Fprintf(w, "# TYPE ids_forwarder_evidence_only_total counter\n")
		evidence := f.stats.evidenceCounts()
		classes := make([]string, 0, len(evidence))
		for class := range evidence {
			classes = append(classes, class)
		}
		sort.Strings(classes)
		for _, class := range classes {
			fmt.Fprintf(w, "ids_forwarder_evidence_only_total{class=%q} %d\n", class, evidence[class])
		}
		fmt.Fprintf(w, "# HELP ids_forwarder_page_candidates_total Silent coverage detections after dedup and coalescing, by class; what enabling that class would have paged.\n")
		fmt.Fprintf(w, "# TYPE ids_forwarder_page_candidates_total counter\n")
		candidates := f.stats.pageCandidateCounts()
		classNames := make([]string, 0, len(candidates))
		for class := range candidates {
			classNames = append(classNames, class)
		}
		sort.Strings(classNames)
		for _, class := range classNames {
			fmt.Fprintf(w, "ids_forwarder_page_candidates_total{class=%q} %d\n", class, candidates[class])
		}
		fmt.Fprintf(w, "# HELP ids_forwarder_provenance_total Classification verdicts by source, counted for every event that reached the layer decision. namespace and namespace+cgroup should dominate; uid and fallback should be replay-only.\n")
		fmt.Fprintf(w, "# TYPE ids_forwarder_provenance_total counter\n")
		prov := f.stats.provenanceCounts()
		provKeys := make([]string, 0, len(prov))
		for key := range prov {
			provKeys = append(provKeys, key)
		}
		sort.Strings(provKeys)
		for _, key := range provKeys {
			check, source, ok := strings.Cut(key, "\x00")
			if !ok {
				continue
			}
			fmt.Fprintf(w, "ids_forwarder_provenance_total{check=%q,source=%q} %d\n", check, source, prov[key])
		}
		fmt.Fprintf(w, "# HELP ids_forwarder_build_info Build metadata; page_schema is the delivery-class schema the binary emits.\n")
		fmt.Fprintf(w, "# TYPE ids_forwarder_build_info gauge\n")
		fmt.Fprintf(w, "ids_forwarder_build_info{page_schema=%q} 1\n", "1")
	})
	srv := &http.Server{Addr: f.cfg.metricsListen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", f.cfg.metricsListen)
	if err != nil {
		return err
	}
	if isNotLoopback(ln.Addr()) {
		ln.Close()
		return errors.New("metrics-listen must bind to a loopback address")
	}
	f.metricsBind = ln.Addr().String()
	f.metricsSrv = srv
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			f.cfg.logf("metrics: %v", err)
		}
	}()
	return nil
}
