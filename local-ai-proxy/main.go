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

func main() {
	addr := flag.String("addr", "", "listen address (overrides config)")
	backendURL := flag.String("backend", "", "single backend URL (overrides config; implies passthrough mode)")
	configPath := flag.String("config", "", "path to YAML config")
	flag.Parse()

	cfg := Config{
		Listen:  ":4000",
		Backend: Backend{URL: "http://localhost:8080"},
	}
	if *configPath != "" {
		loaded, err := LoadConfig(*configPath)
		if err != nil {
			log.Fatal(err)
		}
		cfg = loaded
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

	h := NewHandler(router)
	h.Health = health
	srv := &http.Server{
		Addr:    cfg.Listen,
		Handler: LoggingMiddleware(logger, h),
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
