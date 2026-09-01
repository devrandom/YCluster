package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// DefaultConfigPath is the config file read by both `serve` and the
// ops subcommands (`models`, `backends`) when -config is not given.
const DefaultConfigPath = "/etc/local-ai-proxy/config.yaml"

// parseArgs handles the subcommand dispatch and config-path prefix. Returns
// the remaining args for serve mode and the resolved config path. Returns
// ok=false when the process should exit (either a CLI subcommand ran or
// usage was printed).
func parseArgs(args []string) (serveArgs []string, configPath string, ok bool) {
	configPath = DefaultConfigPath
	for len(args) >= 2 && (args[0] == "-c" || args[0] == "--config") {
		configPath = args[1]
		args = args[2:]
	}
	if len(args) > 0 {
		switch args[0] {
		case "models", "backends", "acl":
			runCLI(args[0], args[1:], configPath)
			return nil, "", false
		case "serve":
			return args[1:], configPath, true
		default:
			usage()
			os.Exit(1)
		}
	}
	usage()
	os.Exit(1)
	return nil, "", false
}

func usage() {
	fmt.Fprintf(os.Stderr, "usage: local-ai-proxy [-c <path>|--config <path>] <subcommand>\n")
}

// mustServeConfig parses serve-mode flags and loads the config. Fatal on error.
func mustServeConfig(configPath string, args []string) (Config, *slog.Logger, *Metrics) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", "", "listen address (overrides config)")
	backendURL := fs.String("backend", "", "single backend URL (overrides config; implies passthrough mode)")
	cfgFlag := fs.String("config", "", "path to YAML config (default "+DefaultConfigPath+")")
	_ = fs.Parse(args)
	if *cfgFlag != "" {
		configPath = *cfgFlag
	}

	cfg, err := LoadConfigOrDefault(configPath, *addr, *backendURL)
	if err != nil {
		log.Fatal(err)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	return cfg, logger, NewMetrics()
}

// startHealthChecker starts the model-health checker only in model-routed
// modes and when an interval is configured. It returns nil in passthrough mode.
func startHealthChecker(cfg Config, source Source, metrics *Metrics, logger *slog.Logger) *HealthChecker {
	if source == nil {
		return nil
	}

	interval := cfg.HealthCheckInterval
	if interval == 0 {
		interval = DefaultHealthCheckInterval
	}
	if interval <= 0 {
		return nil
	}

	health := NewHealthChecker(source, interval, logger)
	health.Metrics = metrics
	configureDisabledBackends(health, source, cfg, logger)
	health.Start(context.Background())
	logger.Info("health checker started", "interval", interval.String())
	return health
}

// configureDisabledBackends wires the disabled-backends feature when the
// Source is etcd-backed: we reuse its client and default the prefix to
// /cluster/config/inference/disabled/.
func configureDisabledBackends(health *HealthChecker, source Source, cfg Config, logger *slog.Logger) {
	es, ok := source.(*EtcdSource)
	if !ok || cfg.Etcd == nil {
		return
	}

	dp := cfg.Etcd.DisabledPrefix
	if dp == "" {
		dp = DefaultDisabledPrefix
	}
	health.Disabled = NewEtcdDisabledBackends(es.Client(), dp, logger)
	logger.Info("disabled-backends tracker enabled", "prefix", dp)
}

// configureRouter attaches health and load state to the model router when it
// is present. Passthrough mode has a single backend and needs neither.
func configureRouter(router Router, health *HealthChecker, loadCounter *LoadCounter) {
	mr, ok := router.(*ModelRouter)
	if !ok {
		return
	}
	if health != nil {
		mr.Healthy = health
	}
	mr.Load = loadCounter
}

// configureHandler wires metrics, ACLs, and load counters into the API
// handler according to the selected routing mode.
func configureHandler(h *Handler, cfg Config, router Router, source Source, health *HealthChecker, metrics *Metrics, loadCounter *LoadCounter) {
	h.Health = health
	h.AuthHealthURL = cfg.AuthHealthURL
	h.Metrics = metrics

	if es, ok := source.(*EtcdSource); ok {
		// Hot-reloading union: yaml acl ⊎ inline-per-model + etcd default.
		yamlACL := cfg.ACL
		h.ACLProvider = func() *ACLConfig { return MergeACL(yamlACL, es.ACLSnapshot()) }
	} else {
		h.ACL = cfg.ACL
	}

	if _, ok := router.(*ModelRouter); ok {
		h.Load = loadCounter
	}
}

// buildServers creates the API listener and, when configured, the separate
// Prometheus listener. Metrics are plain text and intentionally bypass the
// logging and trusted-header middleware.
func buildServers(cfg Config, chained http.Handler, metrics *Metrics, logger *slog.Logger) []*http.Server {
	mux := http.NewServeMux()
	if cfg.MetricsListen == "" {
		mux.Handle("/metrics", metrics.Handler())
	}
	mux.Handle("/", chained)

	servers := []*http.Server{{Addr: cfg.Listen, Handler: mux}}
	if cfg.MetricsListen != "" {
		mmux := http.NewServeMux()
		mmux.Handle("/metrics", metrics.Handler())
		servers = append(servers, &http.Server{Addr: cfg.MetricsListen, Handler: mmux})
		logger.Info("metrics served on separate listener", "addr", cfg.MetricsListen, "main", cfg.Listen)
	}
	return servers
}

// runServers starts all listeners and blocks until a listener fails or a
// graceful shutdown completes after SIGINT/SIGTERM.
func runServers(servers []*http.Server, logger *slog.Logger) {
	errCh := make(chan error, len(servers))
	startListeners(servers, errCh)

	shutdownDone := make(chan struct{})
	go shutdownOnSignal(servers, logger, shutdownDone)
	awaitServerExit(servers, errCh, shutdownDone, logger)
}

func startListeners(servers []*http.Server, errCh chan<- error) {
	for _, s := range servers {
		s := s
		go func() {
			if err := s.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				errCh <- err
			}
		}()
	}
}

func shutdownOnSignal(servers []*http.Server, logger *slog.Logger, done chan<- struct{}) {
	defer close(done)
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigCh
	logger.Info("shutdown requested", "signal", sig.String())

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for _, s := range servers {
		if err := s.Shutdown(ctx); err != nil {
			logger.Warn("graceful shutdown timed out; forcing close", "err", err.Error())
			_ = s.Close()
		}
	}
}

func awaitServerExit(servers []*http.Server, errCh <-chan error, shutdownDone <-chan struct{}, logger *slog.Logger) {
	select {
	case err := <-errCh:
		logger.Error("listener exited unexpectedly", "err", err)
		for _, s := range servers {
			_ = s.Close()
		}
		os.Exit(1)
	case <-shutdownDone:
	}
}

// buildRouter selects the routing mode from config and returns a router
// plus an optional Source whose lifecycle the caller must close on
// shutdown. Validate is expected to have already enforced exactly one
// source being set.
func buildRouter(cfg Config, logger *slog.Logger) (Router, Source, error) {
	switch {
	case cfg.Backend.URL != "":
		return buildPassthroughRouter(cfg, logger)
	case len(cfg.Backends) > 0:
		return buildYAMLRouter(cfg, logger)
	case cfg.Etcd != nil && cfg.Etcd.Prefix != "":
		return buildEtcdRouter(cfg, logger)
	}
	return nil, nil, errors.New("no routing source configured (this is a bug; Validate should have caught it)")
}

func buildPassthroughRouter(cfg Config, logger *slog.Logger) (Router, Source, error) {
	b, err := url.Parse(cfg.Backend.URL)
	if err != nil {
		return nil, nil, err
	}
	logger.Info("local-ai-proxy starting", "mode", "passthrough", "listen", cfg.Listen, "backend", b.String())
	return NewPassthroughRouter(b), nil, nil
}

func buildYAMLRouter(cfg Config, logger *slog.Logger) (Router, Source, error) {
	src, err := NewYAMLSource(cfg.Backends)
	if err != nil {
		return nil, nil, err
	}
	if err := src.Start(context.Background()); err != nil {
		return nil, nil, err
	}
	logger.Info("local-ai-proxy starting", "mode", "yaml", "listen", cfg.Listen, "models", len(cfg.Backends), "max_routing_body_bytes", cfg.MaxRoutingBodyBytes)
	return newYAMLModelRouter(cfg, src, logger), src, nil
}

func buildEtcdRouter(cfg Config, logger *slog.Logger) (Router, Source, error) {
	src, err := NewEtcdSource(*cfg.Etcd, logger)
	if err != nil {
		return nil, nil, err
	}
	startCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := src.Start(startCtx); err != nil {
		_ = src.Close()
		return nil, nil, err
	}
	// Log the resolved endpoints (ETCD_HOSTS env wins over YAML), not the
	// raw YAML field, which is usually empty in production.
	logger.Info("local-ai-proxy starting", "mode", "etcd", "listen", cfg.Listen, "endpoints", etcdEndpoints(cfg.Etcd), "prefix", cfg.Etcd.Prefix, "max_routing_body_bytes", cfg.MaxRoutingBodyBytes)
	return newYAMLModelRouter(cfg, src, logger), src, nil
}

func newYAMLModelRouter(cfg Config, src Source, logger *slog.Logger) *ModelRouter {
	r := NewModelRouter(src)
	r.MaxRoutingBodyBytes = cfg.MaxRoutingBodyBytes
	r.Logger = logger
	return r
}
