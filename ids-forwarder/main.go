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
	flag.StringVar(&cfg.httpListen, "http-listen",
		cfg.httpListen, "optional localhost HTTP receiver for CrowdSec notifications")
	flag.StringVar(&cfg.metricsListen, "metrics-listen",
		cfg.metricsListen, "Prometheus metrics listen address (empty disables)")
	flag.StringVar(&cfg.hostname, "hostname",
		cfg.hostname, "host label (defaults to the local hostname)")
	flag.IntVar(&cfg.queueMax, "queue-max",
		cfg.queueMax, "maximum queued alerts before dropping the oldest")
	flag.DurationVar(&cfg.dedupWindow, "dedup-window",
		cfg.dedupWindow, "suppress repeat alerts with the same key within this window")
	flag.DurationVar(&cfg.alertTTL, "alert-ttl",
		cfg.alertTTL, "alerts auto-resolve after this duration")
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
