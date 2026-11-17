package main

import (
	"fmt"
	"log"
	"net/netip"
	"strings"
	"time"
)

const (
	sourceTetragon = "tetragon"
	sourceCrowdSec = "crowdsec"
	alertName      = "IDSAlert"
)

type config struct {
	alertmanagerURL  string
	tetragonLog      string
	events           string
	execBinaries     string
	execRootUserPath bool
	httpListen       string
	metricsListen    string
	hostname         string
	queueMax         int
	dedupWindow      time.Duration
	rateLimit        int
	rateWindow       time.Duration
	alertTTL         time.Duration
	maxEventAge      time.Duration
	startupGrace     time.Duration
	bootGrace        time.Duration
	cursorFile       string
	spoolFile        string
	spoolMaxBytes    int64
	detectionsFile   string
	detectionsBytes  int64
	detectionsKeep   int
	destKeyFile      string
	sigmaSocket      string
	sigmaDetections  string
	sigmaCursorFile  string
	namespaceMap     string
	serviceAddresses []netip.Addr
	postTimeout      time.Duration
	pollInterval     time.Duration
	logf             func(format string, args ...any)
}

func defaultConfig() config {
	return config{
		alertmanagerURL: "http://127.0.0.1:9093",
		tetragonLog:     "/var/log/tetragon/tetragon.log",
		events:          "process_kprobe",
		execBinaries:    "bash,sh,dash,zsh,fish,sudo,su,apt,apt-get,dpkg,snap,curl,wget,incus",
		httpListen:      "",
		metricsListen:   "127.0.0.1:2113",
		queueMax:        1000,
		dedupWindow:     5 * time.Minute,
		rateLimit:       0,
		rateWindow:      time.Hour,
		alertTTL:        10 * time.Minute,
		maxEventAge:     24 * time.Hour,
		startupGrace:    time.Minute,
		bootGrace:       5 * time.Minute,
		spoolMaxBytes:   100 << 20,
		detectionsFile:  "/var/lib/ids-forwarder/detections.jsonl",
		detectionsBytes: 50 << 20,
		detectionsKeep:  10,
		destKeyFile:     "/var/lib/ids-forwarder/dest.key",
		postTimeout:     5 * time.Second,
		pollInterval:    500 * time.Millisecond,
		logf:            log.Printf,
	}
}

// parseServiceAddresses reads the comma-separated cluster service entry
// points. A field that does not parse is an error: a typo would otherwise
// re-open the dials the list exists to suppress, silently.
func parseServiceAddresses(value string) ([]netip.Addr, error) {
	var out []netip.Addr
	for _, field := range strings.Split(value, ",") {
		field = strings.TrimSpace(field)
		if field == "" {
			continue
		}
		addr, err := netip.ParseAddr(field)
		if err != nil {
			return nil, fmt.Errorf("%q: %w", field, err)
		}
		out = append(out, addr)
	}
	return out, nil
}
