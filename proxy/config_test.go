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

func TestValidateMissingBackendURL(t *testing.T) {
	err := Config{Listen: ":4000"}.Validate()
	if err == nil || !strings.Contains(err.Error(), "backend.url") {
		t.Errorf("want backend.url error, got %v", err)
	}
}
