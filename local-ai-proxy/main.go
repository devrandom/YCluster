package main

import (
	"context"
	"errors"
	"flag"
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

func main() {
	// Subcommand dispatch. `-config <path>` may appear before the
	// subcommand (so ops commands use the same config as the server).
	args := os.Args[1:]
	configPath := DefaultConfigPath
	for len(args) >= 2 && args[0] == "-config" {
		configPath = args[1]
		args = args[2:]
	}
	if len(args) > 0 {
		switch args[0] {
		case "models", "backends":
			runCLI(args[0], args[1:], configPath)
			return
		case "serve":
			args = args[1:]
		}
	}

	// Serve mode. Parse the remaining flags as before.
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", "", "listen address (overrides config)")
	backendURL := fs.String("backend", "", "single backend URL (overrides config; implies passthrough mode)")
	cfgFlag := fs.String("config", "", "path to YAML config (default "+DefaultConfigPath+")")
	_ = fs.Parse(args)
	if *cfgFlag != "" {
		configPath = *cfgFlag
	}

	cfg := Config{
		Listen:  ":4000",
		Backend: Backend{URL: "http://localhost:8080"},
	}
	if _, err := os.Stat(configPath); err == nil {
		loaded, err := LoadConfig(configPath)
		if err != nil {
			log.Fatal(err)
		}
		cfg = loaded
	} else if !errors.Is(err, os.ErrNotExist) || configPath != DefaultConfigPath {
		// Missing default config falls back to the built-in defaults.
		// An explicit -config path must exist.
		log.Fatalf("config %s: %v", configPath, err)
	}
	if *addr != "" {
		cfg.Listen = *addr
	}
	if *backendURL != "" {
		// --backend overrides to passthrough mode: clear multi-source fields.
		cfg.Backend.URL = *backendURL
		cfg.Backends = nil
		cfg.Etcd = nil
	}
	if err := cfg.Validate(); err != nil {
		log.Fatal(err)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	metrics := NewMetrics()

	router, source, err := buildRouter(cfg, logger)
	if err != nil {
		log.Fatal(err)
	}
	if source != nil {
		defer source.Close()
	}

	// Health checker: only runs in model-routed modes (source != nil)
	// and when interval > 0. Exposes /healthz; not yet wired into
	// routing decisions.
	var health *HealthChecker
	if source != nil {
		interval := cfg.HealthCheckInterval
		if interval == 0 {
			interval = DefaultHealthCheckInterval
		}
		if interval > 0 {
			health = NewHealthChecker(source, interval, logger)
			health.Metrics = metrics
			// Wire the disabled-backends feature when the Source is
			// etcd-backed: we reuse its client, and default the prefix
			// to /cluster/config/inference/disabled/.
			if es, ok := source.(*EtcdSource); ok && cfg.Etcd != nil {
				dp := cfg.Etcd.DisabledPrefix
				if dp == "" {
					dp = DefaultDisabledPrefix
				}
				health.Disabled = NewEtcdDisabledBackends(es.Client(), dp, logger)
				logger.Info("disabled-backends tracker enabled", "prefix", dp)
			}
			health.Start(context.Background())
			defer health.Close()
			logger.Info("health checker started", "interval", interval.String())
		}
	}

	// Fan-out: the LoadCounter is shared between handler (inc/dec around
	// upstream calls) and router (reads counts to pick least-loaded).
	// Passthrough mode skips it — a single backend has nothing to balance.
	loadCounter := NewLoadCounter()
	if mr, ok := router.(*ModelRouter); ok {
		if health != nil {
			mr.Healthy = health
		}
		mr.Load = loadCounter
	}

	h := NewHandler(router)
	h.Health = health
	h.Metrics = metrics
	if _, ok := router.(*ModelRouter); ok {
		h.Load = loadCounter
	}

	// Outer middleware chain: TrustedHeaders strips X-User-Id for
	// requests from outside the trusted proxy CIDRs, so nothing
	// downstream (logging, handler, upstream backend) sees a forged
	// identity.
	var chained http.Handler = LoggingMiddleware(logger, h)
	chained, err = TrustedHeadersMiddleware(cfg.TrustedProxies, chained)
	if err != nil {
		log.Fatal(err)
	}

	// /metrics bypasses the proxy handler; everything else goes through
	// the middleware chain. Metrics are plain Prometheus text and don't
	// need logging or X-User-Id stripping.
	mux := http.NewServeMux()
	mux.Handle("/metrics", metrics.Handler())
	mux.Handle("/", chained)

	srv := &http.Server{
		Addr:    cfg.Listen,
		Handler: mux,
	}

	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		sig := <-sigCh
		logger.Info("shutdown requested", "signal", sig.String())

		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			logger.Warn("graceful shutdown timed out; forcing close", "err", err.Error())
			_ = srv.Close()
		}
	}()

	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
	<-shutdownDone
	logger.Info("stopped")
}

// buildRouter selects the routing mode from config and returns a router
// plus an optional Source whose lifecycle the caller must close on
// shutdown. Validate is expected to have already enforced exactly one
// source being set.
func buildRouter(cfg Config, logger *slog.Logger) (Router, Source, error) {
	switch {
	case cfg.Backend.URL != "":
		b, err := url.Parse(cfg.Backend.URL)
		if err != nil {
			return nil, nil, err
		}
		logger.Info("local-ai-proxy starting", "mode", "passthrough", "listen", cfg.Listen, "backend", b.String())
		return NewPassthroughRouter(b), nil, nil

	case len(cfg.Backends) > 0:
		src, err := NewYAMLSource(cfg.Backends)
		if err != nil {
			return nil, nil, err
		}
		if err := src.Start(context.Background()); err != nil {
			return nil, nil, err
		}
		logger.Info("local-ai-proxy starting", "mode", "yaml", "listen", cfg.Listen, "models", len(cfg.Backends))
		return NewModelRouter(src), src, nil

	case cfg.Etcd != nil && cfg.Etcd.Prefix != "":
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
		logger.Info("local-ai-proxy starting", "mode", "etcd", "listen", cfg.Listen, "endpoints", cfg.Etcd.Endpoints, "prefix", cfg.Etcd.Prefix)
		return NewModelRouter(src), src, nil
	}

	return nil, nil, errors.New("no routing source configured (this is a bug; Validate should have caught it)")
}
