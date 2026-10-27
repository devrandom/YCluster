package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	cfg := defaultConfig()
	flag.StringVar(&cfg.alertmanagerURL, "alertmanager-url",
		cfg.alertmanagerURL, "Alertmanager v2 alerts API base URL")
	flag.StringVar(&cfg.tetragonLog, "tetragon-log",
		cfg.tetragonLog, "Tetragon JSON events file (empty disables the tailer)")
	flag.StringVar(&cfg.events, "events",
		cfg.events, "comma-separated Tetragon event types to forward")
	flag.StringVar(&cfg.execBinaries, "exec-binaries",
		cfg.execBinaries, "comma-separated binary basenames forwarded for process_exec events")
	flag.StringVar(&cfg.httpListen, "http-listen",
		cfg.httpListen, "optional localhost HTTP receiver for CrowdSec notifications")
	flag.StringVar(&cfg.metricsListen, "metrics-listen",
		cfg.metricsListen, "Prometheus metrics listen address (empty disables)")
	flag.StringVar(&cfg.hostname, "hostname",
		cfg.hostname, "host label (defaults to the local hostname)")
	flag.IntVar(&cfg.queueMax, "queue-max",
		cfg.queueMax, "maximum in-memory queued alerts when no spool is configured")
	flag.DurationVar(&cfg.dedupWindow, "dedup-window",
		cfg.dedupWindow, "suppress repeat alerts with the same key within this window")
	flag.DurationVar(&cfg.alertTTL, "alert-ttl",
		cfg.alertTTL, "how long a delivered alert stays active in Alertmanager")
	flag.DurationVar(&cfg.maxEventAge, "max-event-age",
		cfg.maxEventAge, "drop events older than this instead of delivering them")
	flag.DurationVar(&cfg.startupGrace, "startup-grace",
		cfg.startupGrace, "first-run replay window used to cover a process restart")
	flag.StringVar(&cfg.cursorFile, "cursor-file",
		cfg.cursorFile, "durable Tetragon replay cursor (empty disables replay)")
	flag.StringVar(&cfg.spoolFile, "spool-file",
		cfg.spoolFile, "durable outbound alert spool (empty uses the in-memory queue)")
	flag.Int64Var(&cfg.spoolMaxBytes, "spool-max-bytes",
		cfg.spoolMaxBytes, "maximum unacknowledged spool bytes before applying backpressure")
	flag.DurationVar(&cfg.postTimeout, "post-timeout",
		cfg.postTimeout, "per-POST timeout to Alertmanager")
	flag.DurationVar(&cfg.pollInterval, "poll-interval",
		cfg.pollInterval, "how often to poll the event file and the outbound queue")
	flag.Parse()

	if cfg.hostname == "" {
		host, err := os.Hostname()
		if err != nil {
			log.Fatalf("ids-forwarder: resolve hostname: %v", err)
		}
		cfg.hostname = host
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := runWith(ctx, cfg); err != nil {
		log.Fatalf("ids-forwarder: %v", err)
	}
}
