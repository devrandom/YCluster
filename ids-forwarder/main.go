// Command ids-forwarder reads IDS events on a node (Tetragon JSON event
// export, optional CrowdSec HTTP notifications) and forwards a filtered,
// IP-redacted, deduplicated stream to Alertmanager's v2 alerts API.
//
// Design: docs/design/intrusion-detection.md. The forwarder has no inbound
// network listener by default; the optional CrowdSec receiver binds to
// localhost only. Alerts carry host/rule labels, never IPs.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("ids-forwarder: %v", err)
	}
}

func run() error {
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
	flag.Parse()

	if cfg.hostname == "" {
		host, err := os.Hostname()
		if err != nil {
			return fmt.Errorf("resolve hostname: %w", err)
		}
		cfg.hostname = host
	}
	f, err := newForwarder(cfg)
	if err != nil {
		return err
	}
	return f.run()
}
