package main

import (
	"log/slog"
	"testing"
)

func TestBuildRouterRequiresSource(t *testing.T) {
	if _, _, err := buildRouter(Config{}, slog.Default()); err == nil {
		t.Fatal("empty config: want error")
	}
}

func TestBuildPassthroughRouter(t *testing.T) {
	router, source, err := buildPassthroughRouter(
		Config{Backend: Backend{URL: "http://backend:8000"}}, slog.Default())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if router == nil {
		t.Fatal("want router")
	}
	if source != nil {
		t.Fatal("passthrough mode has no source")
	}
}
