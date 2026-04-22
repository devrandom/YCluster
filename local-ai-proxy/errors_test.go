package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestErrorResponseOnUnreachableBackend(t *testing.T) {
	// Stand up a server just to get a valid bound address, then close it.
	// The resulting URL is guaranteed to refuse connections.
	dead := httptest.NewServer(http.NotFoundHandler())
	dead.Close()
	backendURL, _ := url.Parse(dead.URL)

	proxySrv := httptest.NewServer(NewHandler(NewPassthroughRouter(backendURL)))
	defer proxySrv.Close()

	resp, err := http.Get(proxySrv.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status = %d; want %d", resp.StatusCode, http.StatusBadGateway)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q; want application/json*", ct)
	}

	var body openAIError
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Error.Type != "api_error" {
		t.Errorf("error.type = %q; want %q", body.Error.Type, "api_error")
	}
	if body.Error.Message == "" {
		t.Error("error.message should be non-empty")
	}
	// Sanity: we should not leak the raw error text (which includes the
	// backend address/port) through the public message.
	if strings.Contains(body.Error.Message, backendURL.Host) {
		t.Errorf("error.message leaks backend address: %q", body.Error.Message)
	}
}

func TestErrorEnvelopeShape(t *testing.T) {
	rec := httptest.NewRecorder()
	writeOpenAIError(rec, http.StatusTooManyRequests, "rate_limit_error", "slow down")

	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("status = %d; want %d", rec.Code, http.StatusTooManyRequests)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q", ct)
	}

	var raw map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
		t.Fatalf("decode: %v", err)
	}
	errObj, ok := raw["error"].(map[string]any)
	if !ok {
		t.Fatalf("response missing top-level 'error' object: %v", raw)
	}
	if errObj["type"] != "rate_limit_error" {
		t.Errorf("error.type = %v", errObj["type"])
	}
	if errObj["message"] != "slow down" {
		t.Errorf("error.message = %v", errObj["message"])
	}
}
