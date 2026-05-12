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

func TestLoadConfigOrDefaultMissingDefaultPath(t *testing.T) {
	cfg, err := LoadConfigOrDefault(DefaultConfigPath, "", "")
	if err != nil {
		t.Fatalf("missing default config path should fall back to defaults, got %v", err)
	}
	if cfg.Listen != ":4000" {
		t.Errorf("Listen = %q; want %q", cfg.Listen, ":4000")
	}
	if cfg.Backend.URL != "http://localhost:8080" {
		t.Errorf("Backend.URL = %q; want %q", cfg.Backend.URL, "http://localhost:8080")
	}
}

func TestLoadConfigOrDefaultMissingExplicitPathErrors(t *testing.T) {
	path := filepath.Join(t.TempDir(), "does-not-exist.yaml")
	_, err := LoadConfigOrDefault(path, "", "")
	if err == nil {
		t.Fatal("missing explicit config should error")
	}
}

func TestLoadConfigOrDefaultMissingExplicit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "missing.yaml")
	_, err := LoadConfigOrDefault(path, "", "")
	if err == nil {
		t.Fatal("missing explicit config should error")
	}
}

func TestLoadConfigOrDefaultWithOverrides(t *testing.T) {
	path := writeTempFile(t, "c.yaml", `
listen: ":4000"
backend:
  url: "http://localhost:8080"
`)
	cfg, err := LoadConfigOrDefault(path, ":5000", "http://override:9090")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Listen != ":5000" {
		t.Errorf("Listen = %q; want %q", cfg.Listen, ":5000")
	}
	if cfg.Backend.URL != "http://override:9090" {
		t.Errorf("Backend.URL = %q; want %q", cfg.Backend.URL, "http://override:9090")
	}
	if len(cfg.Backends) != 0 {
		t.Errorf("Backends should be cleared by --backend override")
	}
	if cfg.Etcd != nil {
		t.Errorf("Etcd should be cleared by --backend override")
	}
}

func TestLoadConfigOrDefaultMalformed(t *testing.T) {
	path := writeTempFile(t, "bad.yaml", "listen: [unclosed\n")
	_, err := LoadConfigOrDefault(path, "", "")
	if err == nil {
		t.Fatal("malformed config should error")
	}
}

func TestLoadConfigOrDefaultValidationFails(t *testing.T) {
	path := writeTempFile(t, "invalid.yaml", `
listen: ":4000"
`)
	_, err := LoadConfigOrDefault(path, "", "")
	if err == nil || !strings.Contains(err.Error(), "one of") {
		t.Errorf("validation error expected, got %v", err)
	}
}
