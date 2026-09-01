package main

import (
	"net/http/httptest"
	"testing"
)

func TestRewriteAPIPath(t *testing.T) {
	cases := []struct{ in, want string }{
		{"/v1/tokenize", "/tokenize"},
		{"/v1/detokenize", "/detokenize"},
		{"/v1/file_parse", "/file_parse"},
		{"/v1/chat/completions", "/v1/chat/completions"},
		{"/other", "/other"},
	}
	for _, tc := range cases {
		req := httptest.NewRequest("POST", tc.in, nil)
		rewriteAPIPath(req)
		if req.URL.Path != tc.want {
			t.Errorf("rewriteAPIPath(%q) = %q, want %q", tc.in, req.URL.Path, tc.want)
		}
	}
}

func TestFilterModelsByACL(t *testing.T) {
	models := []string{"alpha", "beta", "gamma"}
	acl := &ACLConfig{
		Models: map[string]ACLModelRule{
			"beta": {Entries: []ACLEntry{{Subject: "user:mallory", Decision: ACLDeny}}},
		},
	}

	req := httptest.NewRequest("GET", "/v1/models", nil)
	req.Header.Set("X-User-Id", "alice")
	got := filterModelsByACL(models, acl, req)
	if len(got) != 3 {
		t.Fatalf("expected all models for non-denied user, got %v", got)
	}

	req.Header.Set("X-User-Id", "mallory")
	got = filterModelsByACL(models, acl, req)
	if len(got) != 2 || got[0] != "alpha" || got[1] != "gamma" {
		t.Fatalf("expected alpha+gamma for non-denied user, got %v", got)
	}
}

func TestHandleRouteError(t *testing.T) {
	h := NewHandler(NewPassthroughRouter(mustURL(t, "http://backend.example:8000")))
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/v1/chat/completions", nil)

	if h.handleRouteError(rec, req, nil) {
		t.Fatal("nil error should not be handled")
	}

	rec = httptest.NewRecorder()
	if !h.handleRouteError(rec, req, ErrNoHealthyBackend) {
		t.Fatal("route error should be handled")
	}
	if rec.Code != 503 {
		t.Fatalf("no-healthy-backend status = %d, want 503", rec.Code)
	}

	rec = httptest.NewRecorder()
	if !h.handleRouteError(rec, req, errInvalidRequest) {
		t.Fatal("route error should be handled")
	}
	if rec.Code != 400 {
		t.Fatalf("invalid-request status = %d, want 400", rec.Code)
	}
}

type errTest struct{ msg string }

func (e errTest) Error() string { return e.msg }

var errInvalidRequest = errTest{"unknown model: ghost"}

func TestExtractMultipartModel(t *testing.T) {
	body := "--BOUNDARY\r\n" +
		"Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n" +
		"Content-Type: audio/wav\r\n" +
		"\r\n" +
		"binary-data-here\r\n" +
		"--BOUNDARY\r\n" +
		"Content-Disposition: form-data; name=\"model\"\r\n" +
		"\r\n" +
		"whisper-1\r\n" +
		"--BOUNDARY--\r\n"
	model, err := extractMultipartModel([]byte(body), "BOUNDARY")
	if err != nil {
		t.Fatal(err)
	}
	if model != "whisper-1" {
		t.Fatalf("expected whisper-1, got %q", model)
	}
}

func TestExtractMultipartModelNotFound(t *testing.T) {
	body := "--BOUNDARY\r\n" +
		"Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n" +
		"\r\n" +
		"binary-data\r\n" +
		"--BOUNDARY--\r\n"
	model, err := extractMultipartModel([]byte(body), "BOUNDARY")
	if err != nil {
		t.Fatal(err)
	}
	if model != "" {
		t.Fatalf("expected empty model, got %q", model)
	}
}

func TestExtractMultipartModelMalformed(t *testing.T) {
	// The multipart reader treats non-multipart input as zero parts (EOF),
	// so the caller's "missing model field" check is the actual guard.
	model, err := extractMultipartModel([]byte("not a multipart body"), "BOUNDARY")
	if err != nil {
		t.Fatal(err)
	}
	if model != "" {
		t.Fatalf("expected empty model, got %q", model)
	}
}
