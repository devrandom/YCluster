package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

// fakeSource is a test double for Source that returns a fixed map.
type fakeSource struct{ m map[string][]*url.URL }

func (f *fakeSource) Start(context.Context) error    { return nil }
func (f *fakeSource) Snapshot() map[string][]*url.URL { return f.m }
func (f *fakeSource) Close() error                   { return nil }

// single wraps one URL in a slice for fakeSource test data.
func single(u *url.URL) []*url.URL { return []*url.URL{u} }

func mustURL(t *testing.T, s string) *url.URL {
	t.Helper()
	u, err := url.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func TestPassthroughRouterAlwaysReturnsBackend(t *testing.T) {
	u := mustURL(t, "http://backend.example:8000")
	r := NewPassthroughRouter(u)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"whatever","messages":[]}`))
	candidates, body, err := r.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) != 1 || candidates[0] != u {
		t.Errorf("got %v; want [%v]", candidates, u)
	}
	if body != nil {
		t.Errorf("passthrough should not buffer body")
	}
	if r.Models() != nil {
		t.Errorf("passthrough Models() should be nil (signals unknown)")
	}
}

func TestModelRouterRoutesByModel(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a.example:8000")},
		"beta":  {mustURL(t, "http://b.example:8000")},
	}}
	r := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"beta","messages":[]}`))
	candidates, body, err := r.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) != 1 || candidates[0].String() != "http://b.example:8000" {
		t.Errorf("candidates = %v; want [http://b.example:8000]", candidates)
	}
	if body == nil {
		t.Fatal("ModelRouter must buffer body so caller can retry")
	}
	if !strings.Contains(string(body), `"model":"beta"`) {
		t.Errorf("buffered body = %q; missing model field", body)
	}
}

func TestModelRouterUnknownModel(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{"alpha": {mustURL(t, "http://a:8000")}}}
	r := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"ghost"}`))
	_, _, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "unknown model") {
		t.Errorf("want unknown model error, got %v", err)
	}
}

func TestModelRouterMissingBody(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	req := httptest.NewRequest(http.MethodGet, "/v1/chat/completions", nil)
	req.Body = nil
	_, _, err := r.Route(req)
	if err == nil {
		t.Fatal("want error when body is nil")
	}
}

func TestModelRouterInvalidJSON(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`not json`))
	_, _, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "valid JSON") {
		t.Errorf("want JSON error, got %v", err)
	}
}

func TestModelRouterMissingModelField(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"messages":[]}`))
	_, _, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "model") {
		t.Errorf("want missing-model error, got %v", err)
	}
}

func TestModelRouterBodyTooLarge(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	big := strings.NewReader(strings.Repeat("x", maxRoutingBodyBytes+1))
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", big)
	_, _, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "too large") {
		t.Errorf("want too-large error, got %v", err)
	}
}

func TestModelRouterModelsReturnsSortedKnownNames(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"charlie": {mustURL(t, "http://c:1")},
		"alpha":   {mustURL(t, "http://a:1")},
		"bravo":   {mustURL(t, "http://b:1")},
	}}
	r := NewModelRouter(src)
	got := r.Models()
	want := []string{"alpha", "bravo", "charlie"}
	if len(got) != len(want) {
		t.Fatalf("got %d models; want %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Models()[%d] = %q; want %q", i, got[i], want[i])
		}
	}
}

func TestYAMLSourceLoads(t *testing.T) {
	s, err := NewYAMLSource([]Mapping{
		{Model: "m1", APIBase: "http://h1:8000"},
		{Model: "m2", APIBase: "http://h2:8000"},
	})
	if err != nil {
		t.Fatal(err)
	}
	snap := s.Snapshot()
	if len(snap) != 2 {
		t.Fatalf("got %d entries; want 2", len(snap))
	}
	if len(snap["m1"]) != 1 || snap["m1"][0].String() != "http://h1:8000" {
		t.Errorf("m1 = %v; want [http://h1:8000]", snap["m1"])
	}
}

// TestYAMLSourceAccumulatesDuplicates documents that multiple YAML
// entries with the same `model:` accumulate their api_bases under
// one key. Forward-compatible with future fan-out routing.
func TestYAMLSourceAccumulatesDuplicates(t *testing.T) {
	s, err := NewYAMLSource([]Mapping{
		{Model: "m", APIBase: "http://h1:8000"},
		{Model: "m", APIBase: "http://h2:8000"},
	})
	if err != nil {
		t.Fatal(err)
	}
	urls := s.Snapshot()["m"]
	if len(urls) != 2 {
		t.Fatalf("got %d urls; want 2", len(urls))
	}
	if urls[0].String() != "http://h1:8000" || urls[1].String() != "http://h2:8000" {
		t.Errorf("order not preserved: %v", urls)
	}
}

func TestYAMLSourceRejectsEmptyFields(t *testing.T) {
	_, err := NewYAMLSource([]Mapping{{Model: "", APIBase: "http://h:1"}})
	if err == nil {
		t.Errorf("want error for empty model")
	}
	_, err = NewYAMLSource([]Mapping{{Model: "m", APIBase: ""}})
	if err == nil {
		t.Errorf("want error for empty api_base")
	}
}

// TestHandlerModelRouting verifies the handler picks the right backend
// based on the request body's model field and routes accordingly.
func TestHandlerModelRouting(t *testing.T) {
	var hitA, hitB int
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hitA++
		w.WriteHeader(http.StatusOK)
	}))
	defer a.Close()
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hitB++
		w.WriteHeader(http.StatusOK)
	}))
	defer b.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, a.URL)},
		"beta":  {mustURL(t, b.URL)},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	for _, model := range []string{"alpha", "alpha", "beta"} {
		body := strings.NewReader(`{"model":"` + model + `","messages":[]}`)
		resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json", body)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("model %s: status = %d", model, resp.StatusCode)
		}
	}
	if hitA != 2 {
		t.Errorf("backend A hit count = %d; want 2", hitA)
	}
	if hitB != 1 {
		t.Errorf("backend B hit count = %d; want 1", hitB)
	}
}

func TestHandlerUnknownModelReturns400(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	body := strings.NewReader(`{"model":"ghost"}`)
	resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json", body)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status = %d; want 400", resp.StatusCode)
	}
	var e openAIError
	if err := json.NewDecoder(resp.Body).Decode(&e); err != nil {
		t.Fatal(err)
	}
	if e.Error.Type != "invalid_request_error" {
		t.Errorf("error.type = %q", e.Error.Type)
	}
}

func TestHandlerSynthesizesModelsList(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a:1")},
		"beta":  {mustURL(t, "http://b:1")},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	resp, err := http.Get(proxy.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body struct {
		Object string `json:"object"`
		Data   []struct {
			ID      string `json:"id"`
			Object  string `json:"object"`
			OwnedBy string `json:"owned_by"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Object != "list" {
		t.Errorf("object = %q; want list", body.Object)
	}
	if len(body.Data) != 2 {
		t.Errorf("data has %d entries; want 2", len(body.Data))
	}
	seen := map[string]bool{}
	for _, m := range body.Data {
		seen[m.ID] = true
		if m.Object != "model" {
			t.Errorf("entry object = %q", m.Object)
		}
	}
	for _, want := range []string{"alpha", "beta"} {
		if !seen[want] {
			t.Errorf("synthesized list missing %q", want)
		}
	}
}

// TestHandlerUnknownPathReturns404 verifies that in model-routed mode,
// hitting a non-/v1/ URL returns a clean 404 instead of a 400 from the
// body parser.
func TestHandlerUnknownPathReturns404(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a:1")},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	for _, path := range []string{"/", "/foo/bar", "/admin"} {
		resp, err := http.Get(proxy.URL + path)
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("%s: status = %d; want 404", path, resp.StatusCode)
		}
		var e openAIError
		if err := json.Unmarshal(body, &e); err != nil {
			t.Errorf("%s: decode: %v (body=%s)", path, err, body)
			continue
		}
		if e.Error.Type != "not_found_error" {
			t.Errorf("%s: error.type = %q", path, e.Error.Type)
		}
	}
}

// TestHandlerSendsContentLengthNotChunked verifies that when
// ModelRouter consumes and substitutes the body, the upstream request
// goes out with Content-Length set rather than Transfer-Encoding:
// chunked. Picky backends (mlx-server, some llama.cpp builds) reject
// chunked POSTs with EOF.
func TestHandlerSendsContentLengthNotChunked(t *testing.T) {
	var seenCL, seenTE string
	var seenBody []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenCL = r.Header.Get("Content-Length")
		seenTE = r.Header.Get("Transfer-Encoding")
		seenBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, upstream.URL)},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	body := `{"model":"alpha","messages":[{"role":"user","content":"hi"}]}`
	resp, err := http.Post(proxy.URL+"/v1/chat/completions",
		"application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if seenTE == "chunked" {
		t.Errorf("upstream received Transfer-Encoding: chunked; want Content-Length")
	}
	if seenCL == "" {
		t.Errorf("upstream received no Content-Length header")
	}
	if string(seenBody) != body {
		t.Errorf("upstream body mismatch:\n got: %s\nwant: %s", seenBody, body)
	}
}

// TestHandlerV1PathsAreForwarded verifies we don't 404 on /v1/*
// endpoints the proxy doesn't explicitly know about — the backend
// decides whether the endpoint exists.
func TestHandlerV1PathsAreForwarded(t *testing.T) {
	var gotPath string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, upstream.URL)},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	// /v1/rerank isn't an OpenAI endpoint but many backends support it.
	body := strings.NewReader(`{"model":"alpha","query":"hi","documents":[]}`)
	resp, err := http.Post(proxy.URL+"/v1/rerank", "application/json", body)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d; want 200 (backend forwarded)", resp.StatusCode)
	}
	if gotPath != "/v1/rerank" {
		t.Errorf("upstream got %q; want /v1/rerank", gotPath)
	}
}

// TestPassthroughProxiesModelsUpstream ensures that passthrough mode
// still forwards /v1/models to the backend rather than synthesizing.
func TestPassthroughProxiesModelsUpstream(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("upstream got path %s", r.URL.Path)
		}
		_, _ = io.WriteString(w, `{"from":"upstream"}`)
	}))
	defer upstream.Close()

	proxy := httptest.NewServer(NewHandler(NewPassthroughRouter(mustURL(t, upstream.URL))))
	defer proxy.Close()

	resp, err := http.Get(proxy.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), `"from":"upstream"`) {
		t.Errorf("got %q; want upstream response (not synthesized)", body)
	}
}
