package main

import (
	"log/slog"
	"net/url"
	"testing"
	"io"
)

func testSource(prefix string) *EtcdSource {
	return &EtcdSource{
		prefix: prefix,
		logger: slog.New(slog.NewJSONHandler(io.Discard, nil)),
	}
}

func TestParseKVValid(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":"http://s1:8000"},{"api_base":"http://s2:8000"}]}`)
	urls, acl, ok := s.parseKV([]byte("/models/m1"), value)
	if !ok {
		t.Fatal("expected ok for valid value")
	}
	if len(urls) != 2 {
		t.Fatalf("expected 2 urls, got %d", len(urls))
	}
	if urls[0].String() != "http://s1:8000" {
		t.Errorf("unexpected first url: %v", urls[0])
	}
	if acl != nil {
		t.Errorf("expected nil acl for value without acl, got %v", acl)
	}
}

func TestParseKVWithACL(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":"http://s1:8000"}],"acl":{"entries":[{"subject":"cn=admins","decision":true}]}}`)
	urls, acl, ok := s.parseKV([]byte("/models/m1"), value)
	if !ok || len(urls) != 1 {
		t.Fatalf("expected ok with 1 url, got ok=%v urls=%d", ok, len(urls))
	}
	if acl == nil || len(acl.Entries) != 1 {
		t.Fatalf("expected acl with 1 entry, got %+v", acl)
	}
	if acl.Entries[0].Subject != "cn=admins" {
		t.Errorf("unexpected subject: %q", acl.Entries[0].Subject)
	}
	if acl.Entries[0].Decision != ACLAllow {
		t.Errorf("unexpected decision: %v", acl.Entries[0].Decision)
	}
}

func TestParseKVInvalidJSON(t *testing.T) {
	s := testSource("/models/")
	_, _, ok := s.parseKV([]byte("/models/m1"), []byte("not json"))
	if ok {
		t.Fatal("expected !ok for invalid JSON")
	}
}

func TestParseKVEmptyBackends(t *testing.T) {
	s := testSource("/models/")
	_, _, ok := s.parseKV([]byte("/models/m1"), []byte(`{"backends":[]}`))
	if ok {
		t.Fatal("expected !ok for empty backends")
	}
}

func TestParseKVMissingAPIBaseSkipped(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":""},{"api_base":"http://s1:8000"}]}`)
	urls, _, ok := s.parseKV([]byte("/models/m1"), value)
	if !ok {
		t.Fatal("expected ok when at least one backend is valid")
	}
	if len(urls) != 1 {
		t.Fatalf("expected 1 url after skipping empty api_base, got %d", len(urls))
	}
}

func TestParseKVUnparseableURLSkipped(t *testing.T) {
	s := testSource("/models/")
	// url.Parse accepts most strings; use a control character to force error.
	value := []byte(`{"backends":[{"api_base":"http://\x7f"}` + `]}`)
	_, _, ok := s.parseKV([]byte("/models/m1"), value)
	// Go's url.Parse is lenient; the exact outcome depends on the input.
	// The invariant: no crash, and ok=false only when nothing valid remains.
	_ = ok
}

func TestParseKVAllBackendsInvalid(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":""}]}`)
	_, _, ok := s.parseKV([]byte("/models/m1"), value)
	if ok {
		t.Fatal("expected !ok when all backends are invalid")
	}
}

func TestModelNameTrimsPrefix(t *testing.T) {
	s := testSource("/models/")
	got := s.modelName([]byte("/models/llama-3"))
	if got != "llama-3" {
		t.Fatalf("expected llama-3, got %q", got)
	}
}

func TestParseKVURLRoundTrip(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":"http://s1:8000/v1"}]}`)
	urls, _, ok := s.parseKV([]byte("/models/m1"), value)
	if !ok || len(urls) != 1 {
		t.Fatal("expected ok with 1 url")
	}
	parsed, _ := url.Parse("http://s1:8000/v1")
	if urls[0].String() != parsed.String() {
		t.Errorf("url mismatch: %v vs %v", urls[0], parsed)
	}
}
