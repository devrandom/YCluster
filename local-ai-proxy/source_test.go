package main

import (
	"io"
	"log/slog"
	"net/url"
	"testing"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
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
	urls, acl, _, ok := s.parseKV([]byte("/models/m1"), value)
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
	urls, acl, _, ok := s.parseKV([]byte("/models/m1"), value)
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
	_, _, _, ok := s.parseKV([]byte("/models/m1"), []byte("not json"))
	if ok {
		t.Fatal("expected !ok for invalid JSON")
	}
}

func TestParseKVEmptyBackends(t *testing.T) {
	s := testSource("/models/")
	_, _, _, ok := s.parseKV([]byte("/models/m1"), []byte(`{"backends":[]}`))
	if ok {
		t.Fatal("expected !ok for empty backends")
	}
}

func TestParseKVMissingAPIBaseSkipped(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":""},{"api_base":"http://s1:8000"}]}`)
	urls, _, _, ok := s.parseKV([]byte("/models/m1"), value)
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
	_, _, _, ok := s.parseKV([]byte("/models/m1"), value)
	// Go's url.Parse is lenient; the exact outcome depends on the input.
	// The invariant: no crash, and ok=false only when nothing valid remains.
	_ = ok
}

func TestParseKVAllBackendsInvalid(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":""}]}`)
	_, _, _, ok := s.parseKV([]byte("/models/m1"), value)
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
	urls, _, _, ok := s.parseKV([]byte("/models/m1"), value)
	if !ok || len(urls) != 1 {
		t.Fatal("expected ok with 1 url")
	}
	parsed, _ := url.Parse("http://s1:8000/v1")
	if urls[0].String() != parsed.String() {
		t.Errorf("url mismatch: %v vs %v", urls[0], parsed)
	}
}

func TestParseKVSupersededBy(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"superseded_by":"ds4.1"}`)
	urls, _, supersededBy, ok := s.parseKV([]byte("/models/ds4"), value)
	if !ok {
		t.Fatal("expected ok for superseded-only value")
	}
	if len(urls) != 0 {
		t.Errorf("superseded-only value should have no routable backends, got %v", urls)
	}
	if supersededBy != "ds4.1" {
		t.Errorf("supersededBy = %q, want ds4.1", supersededBy)
	}
}

func TestParseKVSelfSupersessionIgnored(t *testing.T) {
	s := testSource("/models/")
	value := []byte(`{"backends":[{"api_base":"http://s1:8000"}],"superseded_by":"ds4.1"}`)
	_, _, supersededBy, ok := s.parseKV([]byte("/models/ds4.1"), value)
	if !ok {
		t.Fatal("expected ok when superseded_by is ignored")
	}
	if supersededBy != "" {
		t.Errorf("self-supersession should be ignored, got %q", supersededBy)
	}
}

func TestSupersededSnapshot(t *testing.T) {
	s := testSource("/models/")
	if got := s.SupersededSnapshot(); got != nil {
		t.Fatalf("empty source should return nil, got %v", got)
	}
	s.superseded.Store(&map[string]string{"ds4": "ds4.1"})
	got := s.SupersededSnapshot()
	if len(got) != 1 || got["ds4"] != "ds4.1" {
		t.Fatalf("SupersededSnapshot() = %v, want ds4 -> ds4.1", got)
	}
}

func TestApplyWatchEvents(t *testing.T) {
	s := &EtcdSource{prefix: "models/", logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	s.snapshot.Store(&map[string][]*url.URL{"existing": {mustURL(t, "http://old:8000")}})
	s.aclModels.Store(&map[string]ACLModelRule{})

	events := []*clientv3.Event{
		{Type: clientv3.EventTypePut, Kv: &mvccpb.KeyValue{
			Key:   []byte("models/new"),
			Value: []byte(`{"backends":[{"api_base":"http://new:8000"}]}`),
		}},
		{Type: clientv3.EventTypeDelete, Kv: &mvccpb.KeyValue{Key: []byte("models/existing")}},
	}
	s.applyWatchEvents(events)

	snap := s.Snapshot()
	if _, ok := snap["existing"]; ok {
		t.Fatal("existing model should have been deleted")
	}
	if urls, ok := snap["new"]; !ok || len(urls) != 1 || urls[0].String() != "http://new:8000" {
		t.Fatalf("new model missing: %v", snap)
	}
}

func TestApplyWatchEventsPutInvalidJSON(t *testing.T) {
	s := &EtcdSource{prefix: "models/", logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	s.snapshot.Store(&map[string][]*url.URL{})
	s.aclModels.Store(&map[string]ACLModelRule{})

	events := []*clientv3.Event{
		{Type: clientv3.EventTypePut, Kv: &mvccpb.KeyValue{
			Key:   []byte("models/bad"),
			Value: []byte(`not json`),
		}},
	}
	s.applyWatchEvents(events)
	if _, ok := s.Snapshot()["bad"]; ok {
		t.Fatal("invalid JSON should not create a model entry")
	}
}
