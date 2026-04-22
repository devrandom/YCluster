package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// TestClientDisconnectCancelsUpstream is the primary correctness test.
// If it fails, the proxy is not propagating the client's context to the
// upstream request — which is the single motivating feature.
func TestClientDisconnectCancelsUpstream(t *testing.T) {
	gotRequest := make(chan struct{})
	upstreamErr := make(chan error, 1)

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(gotRequest)
		select {
		case <-r.Context().Done():
			upstreamErr <- r.Context().Err()
		case <-time.After(5 * time.Second):
			upstreamErr <- nil
		}
	}))
	defer upstream.Close()

	backendURL, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	proxySrv := httptest.NewServer(NewHandler(backendURL))
	defer proxySrv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, proxySrv.URL+"/anything", nil)
	if err != nil {
		t.Fatal(err)
	}

	clientDone := make(chan struct{})
	go func() {
		defer close(clientDone)
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
	}()

	select {
	case <-gotRequest:
	case <-time.After(2 * time.Second):
		t.Fatal("upstream never received the request")
	}

	cancel()

	select {
	case err := <-upstreamErr:
		if err == nil {
			t.Fatal("upstream handler finished naturally; expected context cancellation")
		}
		if err != context.Canceled {
			t.Fatalf("upstream saw %v; want context.Canceled", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("upstream did not observe context cancellation within 2s of client cancel")
	}

	<-clientDone
}

// TestProxiesResponse is a smoke test that a normal request round-trips.
func TestProxiesResponse(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-From-Backend", "yes")
		w.WriteHeader(http.StatusTeapot)
		_, _ = io.WriteString(w, "hello from backend: "+r.URL.Path)
	}))
	defer upstream.Close()

	backendURL, _ := url.Parse(upstream.URL)
	proxySrv := httptest.NewServer(NewHandler(backendURL))
	defer proxySrv.Close()

	resp, err := http.Get(proxySrv.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusTeapot {
		t.Errorf("status = %d; want %d", resp.StatusCode, http.StatusTeapot)
	}
	if got := resp.Header.Get("X-From-Backend"); got != "yes" {
		t.Errorf("X-From-Backend = %q; want %q", got, "yes")
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), "/v1/models") {
		t.Errorf("body = %q; want path %q to have been forwarded", body, "/v1/models")
	}
}

// TestHopByHopHeadersStripped verifies we don't leak hop-by-hop headers
// (RFC 7230 §6.1) into the upstream request or back to the client.
func TestHopByHopHeadersStripped(t *testing.T) {
	var seenUpstream http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenUpstream = r.Header.Clone()
		w.Header().Set("Keep-Alive", "timeout=5")
		w.Header().Set("X-Payload", "ok")
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	backendURL, _ := url.Parse(upstream.URL)
	proxySrv := httptest.NewServer(NewHandler(backendURL))
	defer proxySrv.Close()

	req, _ := http.NewRequest(http.MethodGet, proxySrv.URL+"/", nil)
	req.Header.Set("X-Keep", "1")
	req.Header.Set("Proxy-Authorization", "should-be-stripped")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if seenUpstream.Get("Proxy-Authorization") != "" {
		t.Error("Proxy-Authorization leaked to upstream")
	}
	if seenUpstream.Get("X-Keep") != "1" {
		t.Error("X-Keep was dropped; non-hop-by-hop headers must pass through")
	}
	if resp.Header.Get("Keep-Alive") != "" {
		t.Error("Keep-Alive leaked back to client")
	}
	if resp.Header.Get("X-Payload") != "ok" {
		t.Error("X-Payload was dropped from response")
	}
}

// TestStreamingIsNotBuffered verifies the proxy flushes upstream chunks to
// the client as they arrive. The upstream writes one event and then parks
// on its own context — if the proxy buffered the response, the client's
// read would block waiting for more data that never comes.
func TestStreamingIsNotBuffered(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		rc := http.NewResponseController(w)
		fmt.Fprint(w, "data: first-chunk\n\n")
		if err := rc.Flush(); err != nil {
			t.Logf("upstream flush: %v", err)
		}
		<-r.Context().Done()
	}))
	defer upstream.Close()

	backendURL, _ := url.Parse(upstream.URL)
	proxySrv := httptest.NewServer(NewHandler(backendURL))
	defer proxySrv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, proxySrv.URL+"/", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	got, err := readChunkWithTimeout(resp.Body, 2*time.Second)
	if err != nil {
		t.Fatalf("client did not receive first chunk: %v", err)
	}
	if !bytes.Contains(got, []byte("first-chunk")) {
		t.Errorf("client received %q; want it to contain %q", got, "first-chunk")
	}
}

// TestClientDisconnectCancelsStreamingUpstream is the streaming analogue
// of TestClientDisconnectCancelsUpstream: verifies that closing the client
// mid-stream cancels the upstream request.
func TestClientDisconnectCancelsStreamingUpstream(t *testing.T) {
	streaming := make(chan struct{})
	upstreamErr := make(chan error, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		rc := http.NewResponseController(w)
		fmt.Fprint(w, "data: hello\n\n")
		if err := rc.Flush(); err != nil {
			t.Logf("upstream flush: %v", err)
		}
		close(streaming)
		select {
		case <-r.Context().Done():
			upstreamErr <- r.Context().Err()
		case <-time.After(5 * time.Second):
			upstreamErr <- nil
		}
	}))
	defer upstream.Close()

	backendURL, _ := url.Parse(upstream.URL)
	proxySrv := httptest.NewServer(NewHandler(backendURL))
	defer proxySrv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, proxySrv.URL+"/", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}

	<-streaming

	if _, err := readChunkWithTimeout(resp.Body, time.Second); err != nil {
		t.Fatalf("did not receive streamed chunk: %v", err)
	}

	resp.Body.Close()
	cancel()

	select {
	case err := <-upstreamErr:
		if err == nil {
			t.Fatal("upstream finished naturally; expected cancellation after client disconnect")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("upstream did not observe cancellation within 2s of client disconnect")
	}
}

// readChunkWithTimeout reads up to 4KB from r, failing if no bytes arrive
// within d. The spawned goroutine may outlive the call if r stays blocked;
// that's acceptable for test hygiene since r will be closed on defer.
func readChunkWithTimeout(r io.Reader, d time.Duration) ([]byte, error) {
	type result struct {
		b   []byte
		err error
	}
	ch := make(chan result, 1)
	go func() {
		buf := make([]byte, 4096)
		n, err := r.Read(buf)
		ch <- result{buf[:n], err}
	}()
	select {
	case res := <-ch:
		return res.b, res.err
	case <-time.After(d):
		return nil, fmt.Errorf("read timed out after %v", d)
	}
}
