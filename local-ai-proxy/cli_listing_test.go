package main

import (
	"os"
	"sort"
	"strings"
	"testing"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
)

func TestParseModelEntries(t *testing.T) {
	kvs := []*mvccpb.KeyValue{
		{Key: []byte("prefix/alpha"), Value: []byte(`{"backends":[{"api_base":"http://a:8000"}]}`)},
		{Key: []byte("prefix/beta"), Value: []byte(`not json`)},
		{Key: []byte("prefix/gamma"), Value: []byte(`{"backends":[{"api_base":"http://g:8000"},{"api_base":"http://g2:8000"}]}`)},
	}
	entries := parseModelEntries(kvs, "prefix/")
	if len(entries) != 2 {
		t.Fatalf("expected 2 valid entries, got %d", len(entries))
	}
	if entries[0].name != "alpha" || entries[0].backends[0] != "http://a:8000" {
		t.Fatalf("unexpected entry: %v", entries[0])
	}
	if entries[1].name != "gamma" || len(entries[1].backends) != 2 {
		t.Fatalf("unexpected entry: %v", entries[1])
	}
}

func TestParseACLEntriesFromKVs(t *testing.T) {
	kvs := []*mvccpb.KeyValue{
		{Key: []byte("prefix/alpha"), Value: []byte(`{"acl":{"entries":[{"subject":"user:alice","decision":true}]}}`)},
		{Key: []byte("prefix/beta"), Value: []byte(`{"backends":[]}`)},
		{Key: []byte("prefix/bad"), Value: []byte(`not json`)},
	}
	entries := parseACLEntriesFromKVs(kvs, "prefix/")
	if len(entries) != 1 {
		t.Fatalf("expected 1 ACL entry, got %d", len(entries))
	}
	if entries[0].model != "alpha" || len(entries[0].entries) != 1 {
		t.Fatalf("unexpected entry: %v", entries[0])
	}
}

func TestPrintModelEntries(t *testing.T) {
	old := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w

	printModelEntries([]modelEntry{
		{name: "alpha", backends: []string{"http://a:8000"}},
		{name: "beta", backends: []string{"http://b1:8000", "http://b2:8000"}},
	})

	w.Close()
	os.Stdout = old
	out := make([]byte, 1024)
	n, _ := r.Read(out)
	text := string(out[:n])
	if !strings.Contains(text, "alpha  ->  http://a:8000") {
		t.Fatalf("single backend format missing: %q", text)
	}
	if !strings.Contains(text, "beta  (2 backends)") {
		t.Fatalf("multi backend format missing: %q", text)
	}
}

func TestPrintACLEntries(t *testing.T) {
	old := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w

	printACLEntries([]modelACLEntry{
		{model: "alpha", entries: []ACLEntry{
			{Subject: "user:alice", Decision: ACLAllow},
			{Subject: "user:mallory", Decision: ACLDeny},
		}},
		{model: "beta", entries: []ACLEntry{
			{Subject: "group:staff", Decision: ACLAllow},
		}},
	})

	w.Close()
	os.Stdout = old
	out := make([]byte, 1024)
	n, _ := r.Read(out)
	text := string(out[:n])
	if !strings.Contains(text, "alpha:\n  +user:alice\n  -user:mallory") {
		t.Fatalf("expected ACL output format, got %q", text)
	}
	if !strings.Contains(text, "beta:\n  +group:staff") {
		t.Fatalf("expected second model output, got %q", text)
	}
}

func TestPrintACLEntriesEmpty(t *testing.T) {
	old := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w

	printACLEntries(nil)

	w.Close()
	os.Stdout = old
	out := make([]byte, 1024)
	n, _ := r.Read(out)
	if !strings.Contains(string(out[:n]), "No per-model ACL rules.") {
		t.Fatalf("expected empty message, got %q", string(out[:n]))
	}
}

func TestParseAndPrintModelEntries(t *testing.T) {
	kvs := []*mvccpb.KeyValue{
		{Key: []byte("prefix/beta"), Value: []byte(`{"backends":[{"api_base":"http://b:8000"},{"api_base":"http://b2:8000"}]}`)},
		{Key: []byte("prefix/alpha"), Value: []byte(`{"backends":[{"api_base":"http://a:8000"}]}`)},
	}
	entries := parseModelEntries(kvs, "prefix/")
	sort.Slice(entries, func(i, j int) bool { return entries[i].name < entries[j].name })

	old := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w
	printModelEntries(entries)
	w.Close()
	os.Stdout = old
	out := make([]byte, 1024)
	n, _ := r.Read(out)
	text := string(out[:n])
	// alpha (sorted first) should appear before beta
	alphaIdx := strings.Index(text, "alpha")
	betaIdx := strings.Index(text, "beta")
	if alphaIdx > betaIdx {
		t.Fatalf("expected alpha before beta in output: %q", text)
	}
}

func TestEtcdConfigured(t *testing.T) {
	if etcdConfigured(Config{}) {
		t.Fatal("empty config: want false")
	}
	if !etcdConfigured(Config{Etcd: &EtcdConfig{Prefix: "/p/"}}) {
		t.Fatal("etcd config: want true")
	}
}
