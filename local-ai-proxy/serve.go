package main

import (
	"log"
	"log/slog"
	"net/http"
	"os"
)

func main() {
	args, configPath, ok := parseArgs(os.Args[1:])
	if !ok {
		return
	}

	cfg, logger, metrics := mustServeConfig(configPath, args)
	chained, cleanup, err := buildServeStack(cfg, logger, metrics)
	if err != nil {
		log.Fatal(err)
	}
	defer cleanup()

	servers := buildServers(cfg, chained, metrics, logger)
	runServers(servers, logger)
	logger.Info("stopped")
}

// buildServeStack wires the router, health checker, handler, and outer
// middleware. The returned cleanup closes resources that may be created
// before an error occurs.
func buildServeStack(cfg Config, logger *slog.Logger, metrics *Metrics) (http.Handler, func(), error) {
	router, source, health, cleanup, err := newServeResources(cfg, logger, metrics)
	if err != nil {
		return nil, nil, err
	}

	chained, err := wireServeHandler(cfg, logger, router, source, health, metrics)
	if err != nil {
		cleanup()
		return nil, nil, err
	}
	return chained, cleanup, nil
}

// newServeResources builds the routing source and health checker. The
// returned cleanup closes resources that may already be running.
func newServeResources(cfg Config, logger *slog.Logger, metrics *Metrics) (Router, Source, *HealthChecker, func(), error) {
	router, source, err := buildRouter(cfg, logger)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	health := startHealthChecker(cfg, source, metrics, logger)
	cleanup := func() {
		if source != nil {
			source.Close()
		}
		if health != nil {
			health.Close()
		}
	}
	return router, source, health, cleanup, nil
}

// wireServeHandler attaches health, metrics, ACLs, and load state, then
// applies the outer middleware chain.
func wireServeHandler(cfg Config, logger *slog.Logger, router Router, source Source, health *HealthChecker, metrics *Metrics) (http.Handler, error) {
	loadCounter := NewLoadCounter()
	configureRouter(router, health, loadCounter)

	h := NewHandler(router)
	configureHandler(h, cfg, router, source, health, metrics, loadCounter)

	chained := LoggingMiddleware(logger, h)
	return TrustedHeadersMiddleware(cfg.TrustedProxies, chained)
}
