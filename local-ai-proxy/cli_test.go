package main

import "testing"

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
