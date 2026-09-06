package main

import (
	"errors"
	"flag"
	"io"
	"os"
	"strings"
	"testing"
)

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = writer
	defer func() { os.Stdout = old }()

	fn()
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}

	var out strings.Builder
	buf := make([]byte, 1024)
	for {
		n, err := reader.Read(buf)
		if err != nil && !errors.Is(err, io.EOF) {
			t.Fatal(err)
		}
		out.Write(buf[:n])
		if n == 0 {
			break
		}
	}
	return out.String()
}

func TestPrintACLSet(t *testing.T) {
	out := captureStdout(t, func() { printACLSet("model", nil) })
	if out != "ACL cleared for model\n" {
		t.Fatalf("unexpected clear output: %q", out)
	}

	rule := &ACLModelRule{Entries: []ACLEntry{
		{Subject: "user:alice", Decision: ACLAllow},
		{Subject: "group:staff", Decision: ACLDeny},
	}}
	out = captureStdout(t, func() { printACLSet("model", rule) })
	want := "ACL updated for model:\n  +user:alice\n  -group:staff\n"
	if out != want {
		t.Fatalf("got %q, want %q", out, want)
	}
}

func TestResolveRemoveArgs(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)

	model, apiBase := resolveRemoveArgs([]string{"https://backend", "model"}, "", fs)
	if model != "model" || apiBase != "https://backend" {
		t.Fatalf("URL-first: got model=%q apiBase=%q", model, apiBase)
	}

	model, apiBase = resolveRemoveArgs([]string{"model"}, "https://backend", fs)
	if model != "model" || apiBase != "https://backend" {
		t.Fatalf("model-first: got model=%q apiBase=%q", model, apiBase)
	}

	model, apiBase = resolveRemoveArgs([]string{}, "", fs)
	if model != "" || apiBase != "" {
		t.Fatalf("empty: got model=%q apiBase=%q", model, apiBase)
	}
}

func TestPopSubcommand(t *testing.T) {
	subcmd, rest := popSubcommand([]string{"disable", "--reason", "x"}, "usage")
	if subcmd != "disable" || len(rest) != 2 || rest[0] != "--reason" {
		t.Fatalf("unexpected subcommand split: %q %v", subcmd, rest)
	}
}

func TestFilterBackends(t *testing.T) {
	backends := []etcdBackendEntry{
		{APIBase: "http://a:8000"},
		{APIBase: "http://b:8000"},
		{APIBase: "http://c:8000"},
	}

	filtered, removed := filterBackends(backends, "http://b:8000")
	if !removed {
		t.Fatal("expected removed=true")
	}
	if len(filtered) != 2 || filtered[0].APIBase != "http://a:8000" || filtered[1].APIBase != "http://c:8000" {
		t.Fatalf("unexpected filtered backends: %v", filtered)
	}

	_, removed = filterBackends(backends, "http://missing:8000")
	if removed {
		t.Fatal("expected removed=false for missing backend")
	}
}
