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
	backendURL := flag.String("backend", "", "backend URL (overrides config)")
	configPath := flag.String("config", "", "path to YAML config (optional; flags override loaded values)")
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
		cfg.Backend.URL = *backendURL
	}
	if err := cfg.Validate(); err != nil {
		log.Fatal(err)
	}

	b, err := url.Parse(cfg.Backend.URL)
	if err != nil {
		log.Fatalf("backend url: %v", err)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	logger.Info("local-ai-proxy starting", "listen", cfg.Listen, "backend", b.String())

	srv := &http.Server{
		Addr:    cfg.Listen,
		Handler: LoggingMiddleware(logger, NewHandler(b)),
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
