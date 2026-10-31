package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const execLine = `{"time":"2026-10-28T12:00:00Z","process_exec":{"process":{"pid":42,"uid":0,"binary":"/bin/sh","arguments":"-c id"}}}`

func TestBridgeWritesNormalizedEvents(t *testing.T) {
	in := strings.NewReader(execLine + "\n" + `{"time":"2026-10-28T12:00:01Z"}` + "\n" + execLine + "\n")
	var out bytes.Buffer
	if err := bridge(in, &out); err != nil {
		t.Fatalf("bridge: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("got %d output lines, want 2: %q", len(lines), out.String())
	}
	if !strings.Contains(lines[0], `"process_exec"`) || !strings.Contains(lines[0], "/bin/sh") {
		t.Fatalf("unexpected normalized event: %s", lines[0])
	}
}

func TestBridgeRejectsMalformedLine(t *testing.T) {
	var out bytes.Buffer
	err := bridge(strings.NewReader("{not json}\n"), &out)
	if err == nil {
		t.Fatal("bridge accepted a malformed line")
	}
	if !strings.Contains(err.Error(), "parse tetragon event") {
		t.Fatalf("err = %v, want parse error", err)
	}
}

func TestRunCopiesFiles(t *testing.T) {
	dir := t.TempDir()
	input := filepath.Join(dir, "in.jsonl")
	output := filepath.Join(dir, "out.ndjson")
	if err := os.WriteFile(input, []byte(execLine+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	if err := run([]string{"-input", input, "-output", output}, strings.NewReader(""), bytes.NewBuffer(nil), &stderr); err != nil {
		t.Fatalf("run: %v (stderr %s)", err, stderr.String())
	}
	got, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "/bin/sh") {
		t.Fatalf("output = %q", got)
	}
}

func TestRunReportsMissingInput(t *testing.T) {
	var stderr bytes.Buffer
	err := run([]string{"-input", filepath.Join(t.TempDir(), "absent.jsonl")}, strings.NewReader(""), bytes.NewBuffer(nil), &stderr)
	if err == nil || !strings.Contains(err.Error(), "open input") {
		t.Fatalf("err = %v, want open input failure", err)
	}
}

func TestRunRejectsUnknownFlag(t *testing.T) {
	var stderr bytes.Buffer
	if err := run([]string{"-nope"}, strings.NewReader(""), bytes.NewBuffer(nil), &stderr); err == nil {
		t.Fatal("run accepted an unknown flag")
	}
}
