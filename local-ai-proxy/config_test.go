package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeTempFile(t *testing.T, name, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadConfig(t *testing.T) {
	path := writeTempFile(t, "c.yaml", `
listen: ":4000"
backend:
  url: "http://localhost:8080"
`)
	cfg, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Listen != ":4000" {
		t.Errorf("Listen = %q; want %q", cfg.Listen, ":4000")
	}
	if cfg.Backend.URL != "http://localhost:8080" {
		t.Errorf("Backend.URL = %q; want %q", cfg.Backend.URL, "http://localhost:8080")
	}
	if err := cfg.Validate(); err != nil {
		t.Errorf("Validate: %v", err)
	}
}

func TestLoadConfigMissingFile(t *testing.T) {
	_, err := LoadConfig(filepath.Join(t.TempDir(), "does-not-exist.yaml"))
	if err == nil {
		t.Fatal("want error for missing file")
	}
}

func TestLoadConfigMalformed(t *testing.T) {
	// Unclosed flow sequence — unambiguously invalid YAML.
	path := writeTempFile(t, "bad.yaml", "listen: [unclosed\n")
	_, err := LoadConfig(path)
	if err == nil {
		t.Fatal("want error for malformed YAML")
	}
}

func TestLoadConfigUnknownField(t *testing.T) {
	// yaml.v3 ignores unknown fields by default — this test documents that
	// behavior rather than asserting a failure. If we later enable strict
	// mode, flip the expectation.
	path := writeTempFile(t, "extra.yaml", `
listen: ":4000"
backend:
  url: "http://x"
unknown_future_field: 42
`)
	_, err := LoadConfig(path)
	if err != nil {
		t.Fatalf("unexpected error on unknown field: %v", err)
	}
}

func TestValidateMissingListen(t *testing.T) {
	err := Config{Backend: Backend{URL: "http://x"}}.Validate()
	if err == nil || !strings.Contains(err.Error(), "listen") {
		t.Errorf("want listen error, got %v", err)
	}
}

func TestValidateNoSourceSet(t *testing.T) {
	err := Config{Listen: ":4000"}.Validate()
	if err == nil || !strings.Contains(err.Error(), "one of") {
		t.Errorf("want no-source error, got %v", err)
	}
}

func TestValidateMultipleSources(t *testing.T) {
	cfg := Config{
		Listen:   ":4000",
		Backend:  Backend{URL: "http://x"},
		Backends: []Mapping{{Model: "m", APIBase: "http://y"}},
	}
	err := cfg.Validate()
	if err == nil || !strings.Contains(err.Error(), "only one of") {
		t.Errorf("want multi-source error, got %v", err)
	}
}

func TestValidateEtcdMissingPrefix(t *testing.T) {
	cfg := Config{
		Listen: ":4000",
		Etcd:   &EtcdConfig{Endpoints: []string{"http://localhost:2379"}},
	}
	err := cfg.Validate()
	if err == nil || !strings.Contains(err.Error(), "etcd.prefix") {
		t.Errorf("want etcd.prefix error, got %v", err)
	}
}

func TestValidateAcceptsBackends(t *testing.T) {
	cfg := Config{
		Listen:   ":4000",
		Backends: []Mapping{{Model: "m", APIBase: "http://y"}},
	}
	if err := cfg.Validate(); err != nil {
		t.Errorf("Validate: %v", err)
	}
}

func TestValidateAcceptsEtcd(t *testing.T) {
	cfg := Config{
		Listen: ":4000",
		Etcd: &EtcdConfig{
			Endpoints: []string{"http://localhost:2379"},
			Prefix:    "/models/",
		},
	}
	if err := cfg.Validate(); err != nil {
		t.Errorf("Validate: %v", err)
	}
}

func TestValidateEtcdWithoutEndpoints(t *testing.T) {
	// Endpoints is defaulted at source construction; validate only
	// requires prefix.
	cfg := Config{
		Listen: ":4000",
		Etcd:   &EtcdConfig{Prefix: "/models/"},
	}
	if err := cfg.Validate(); err != nil {
		t.Errorf("Validate: %v", err)
	}
}
