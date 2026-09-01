package main

import (
	"io"
	"log/slog"
	"net/http"
	"testing"
)

func TestConfigureRouterAndHandler(t *testing.T) {
	src, err := NewYAMLSource([]Mapping{{Model: "m", APIBase: "http://backend"}})
	if err != nil {
		t.Fatal(err)
	}
	router := NewModelRouter(src)
	health := NewHealthChecker(src, DefaultHealthCheckInterval, nil)
	load := NewLoadCounter()
	configureRouter(router, health, load)

	if router.Healthy != health {
		t.Fatal("router health checker was not attached")
	}
	if router.Load != load {
		t.Fatal("router load counter was not attached")
	}

	cfg := Config{AuthHealthURL: "http://auth", ACL: &ACLConfig{}}
	metrics := NewMetrics()
	h := NewHandler(router)
	configureHandler(h, cfg, router, src, health, metrics, load)

	if h.Health != health || h.Metrics != metrics || h.Load != load {
		t.Fatal("handler dependencies were not attached")
	}
	if h.AuthHealthURL != cfg.AuthHealthURL {
		t.Fatal("auth health URL was not attached")
	}
	if h.ACL != cfg.ACL {
		t.Fatal("static ACL was not attached")
	}

	passthrough := NewPassthroughRouter(mustURL(t, "http://backend"))
	h = NewHandler(passthrough)
	configureHandler(h, cfg, passthrough, nil, nil, metrics, load)
	if h.ACL != cfg.ACL || h.Load != nil {
		t.Fatal("passthrough handler configuration does not match expected mode")
	}
}

func TestStartHealthCheckerNilSource(t *testing.T) {
	if got := startHealthChecker(Config{}, nil, NewMetrics(), nil); got != nil {
		t.Fatal("nil source should disable health checking")
	}
}

func TestBuildServersMetricsListen(t *testing.T) {
	cfg := Config{Listen: ":0", MetricsListen: ":0"}
	servers := buildServers(cfg, http.NotFoundHandler(), NewMetrics(), testLogger())
	if len(servers) != 2 {
		t.Fatalf("got %d servers, want 2", len(servers))
	}

	cfg.MetricsListen = ""
	servers = buildServers(cfg, http.NotFoundHandler(), NewMetrics(), testLogger())
	if len(servers) != 1 || servers[0].Handler == nil {
		t.Fatalf("default metrics mode should create one configured server, got %#v", servers)
	}
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestParseArgsServe(t *testing.T) {
	args, cfg, ok := parseArgs([]string{"serve", "--addr", ":8080"})
	if !ok {
		t.Fatal("expected ok=true for serve")
	}
	if cfg != DefaultConfigPath {
		t.Fatalf("config path = %q, want default", cfg)
	}
	if len(args) != 2 || args[0] != "--addr" {
		t.Fatalf("unexpected serve args: %v", args)
	}
}

func TestParseArgsConfigPrefix(t *testing.T) {
	_, cfg, ok := parseArgs([]string{"-c", "/tmp/test.yaml", "serve"})
	if !ok {
		t.Fatal("expected ok=true")
	}
	if cfg != "/tmp/test.yaml" {
		t.Fatalf("config path = %q, want /tmp/test.yaml", cfg)
	}
}
