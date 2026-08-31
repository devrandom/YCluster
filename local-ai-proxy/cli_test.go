package main

import (
	"flag"
	"strings"
	"testing"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
)

func TestDisabledPrefixFor(t *testing.T) {
	// Unset DisabledPrefix must resolve to the same default the gateway uses
	// (main.go), or `backends disable` writes where the gateway never reads.
	if got := disabledPrefixFor(&EtcdConfig{Prefix: "/cluster/config/inference/models/"}); got != DefaultDisabledPrefix {
		t.Errorf("unset DisabledPrefix = %q, want default %q", got, DefaultDisabledPrefix)
	}
	if got := disabledPrefixFor(&EtcdConfig{Prefix: "/m/", DisabledPrefix: "/custom/disabled/"}); got != "/custom/disabled/" {
		t.Errorf("explicit DisabledPrefix = %q, want /custom/disabled/", got)
	}
	// The disabled prefix must never collide with the models prefix, or a
	// disable lands among the models (the bug this guards against).
	if disabledPrefixFor(&EtcdConfig{Prefix: "/cluster/config/inference/models/"}) == "/cluster/config/inference/models/" {
		t.Error("disabled prefix resolved to the models prefix")
	}
}

func TestValidateModelName(t *testing.T) {
	bad := []string{"", "   ", "http://nv2.xc:8000", "https://x", "grpc://y:9000"}
	for _, m := range bad {
		if err := validateModelName(m); err == nil {
			t.Errorf("validateModelName(%q) = nil, want error", m)
		}
	}
	good := []string{"qwen3.5-a3b", "nvidia/MiniMax-M2.7-NVFP4", "nvidia/MiniMax-M2.7-NVFP4/US", "mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8", "large-v3", "whisper-1"}
	for _, m := range good {
		if err := validateModelName(m); err != nil {
			t.Errorf("validateModelName(%q) = %v, want nil", m, err)
		}
	}
}

func TestNormalizeBackendURL(t *testing.T) {
	// A model name ("org/model") parses as host+path; truncating it to the host
	// lands a junk entry matching no real backend, so reject it instead.
	bad := []string{"", "   ", "brandonmusic/minimax-m3-nvfp4", "MiniMaxAI/MiniMax-M3", "nv4.xc:9211/v1", "http://nv4.xc:9211/v1/models", "http://nv4.xc?x=1", "http://nv4.xc#f"}
	for _, s := range bad {
		if got, err := normalizeBackendURL(s); err == nil {
			t.Errorf("normalizeBackendURL(%q) = %q, nil; want error", s, got)
		}
	}
	good := map[string]string{
		"http://nv4.xc:9211":  "http://nv4.xc:9211",
		"nv4.xc:9211":         "http://nv4.xc:9211", // scheme defaulted
		"http://nv4.xc:9211/": "http://nv4.xc:9211", // bare trailing slash allowed
		"https://x1.xc:8080":  "https://x1.xc:8080",
	}
	for in, want := range good {
		if got, err := normalizeBackendURL(in); err != nil || got != want {
			t.Errorf("normalizeBackendURL(%q) = %q, %v; want %q, nil", in, got, err, want)
		}
	}
}

func TestParseModelsAddArgs(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	apiBase, model, err := parseModelsAddArgs([]string{"http://backend:8000", "model"}, fs)
	if err != nil || apiBase != "http://backend:8000" || model != "model" {
		t.Fatalf("got %q, %q, %v", apiBase, model, err)
	}

	apiBase, model, err = parseModelsAddArgs([]string{"backend:8000"}, fs)
	if err != nil || apiBase != "http://backend:8000" || model != "" {
		t.Fatalf("discover: got %q, %q, %v", apiBase, model, err)
	}

	if _, _, err := parseModelsAddArgs(nil, fs); err == nil {
		t.Fatal("empty args: want error")
	}
	if _, _, err := parseModelsAddArgs([]string{"http://backend:8000", "http://bad"}, fs); err == nil {
		t.Fatal("URL model: want error")
	}
}

func TestPrintDisabledBackend(t *testing.T) {
	kv := &mvccpb.KeyValue{Key: []byte("/prefix/http://backend:8000"), Value: []byte("note")}
	out := captureStdout(t, func() { printDisabledBackend("/prefix/", kv) })
	if out != "  http://backend:8000  note\n" {
		t.Fatalf("got %q", out)
	}

	kv.Value = nil
	out = captureStdout(t, func() { printDisabledBackend("/prefix/", kv) })
	if out != "  http://backend:8000\n" {
		t.Fatalf("got %q", out)
	}
}

func TestDisableTarget(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	if got := disableTarget([]string{"http://backend:8000"}, fs); got != "http://backend:8000" {
		t.Fatalf("got %q", got)
	}
}

func TestACLRules(t *testing.T) {
	if aclRule(nil, 0) != nil {
		t.Fatal("empty tokens: want nil rule")
	}
	entries := []ACLEntry{{Subject: "user:a"}}
	if aclRule(entries, 1).Entries[0].Subject != "user:a" {
		t.Fatal("entry not retained")
	}
}

func TestPrintDisabled(t *testing.T) {
	out := captureStdout(t, func() { printDisabled("bad", "http://b") })
	if !strings.Contains(out, "Disabled: http://b") || !strings.Contains(out, "reason: bad") {
		t.Fatalf("got %q", out)
	}
	out = captureStdout(t, func() { printDisabled("", "http://b") })
	if strings.Contains(out, "reason:") {
		t.Fatalf("got %q", out)
	}
}
