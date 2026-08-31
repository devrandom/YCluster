package main

import (
	"errors"
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
