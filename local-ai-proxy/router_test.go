package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"net/url"
	"strings"
	"testing"
)

// fakeSource is a test double for Source that returns a fixed map.
type fakeSource struct{ m map[string][]*url.URL }

func (f *fakeSource) Start(context.Context) error    { return nil }
func (f *fakeSource) Snapshot() map[string][]*url.URL { return f.m }
func (f *fakeSource) Close() error                   { return nil }

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
	got, err := r.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Candidates) != 1 || got.Candidates[0] != u {
		t.Errorf("got %v; want [%v]", got.Candidates, u)
	}
	if got.Body != nil {
		t.Errorf("passthrough should not buffer body")
	}
	if got.Model != "" {
		t.Errorf("passthrough Model = %q; want empty", got.Model)
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
	got, err := r.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Candidates) != 1 || got.Candidates[0].String() != "http://b.example:8000" {
		t.Errorf("candidates = %v; want [http://b.example:8000]", got.Candidates)
	}
	if got.Body == nil {
		t.Fatal("ModelRouter must buffer body so caller can retry")
	}
	if !strings.Contains(string(got.Body), `"model":"beta"`) {
		t.Errorf("buffered body = %q; missing model field", got.Body)
	}
	if got.Model != "beta" {
		t.Errorf("Model = %q; want beta", got.Model)
	}
}

func TestModelRouterUnknownModel(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{"alpha": {mustURL(t, "http://a:8000")}}}
	r := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"ghost"}`))
	_, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "unknown model") {
		t.Errorf("want unknown model error, got %v", err)
	}
}

func TestModelRouterMissingBody(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	req := httptest.NewRequest(http.MethodGet, "/v1/chat/completions", nil)
	req.Body = nil
	_, err := r.Route(req)
	if err == nil {
		t.Fatal("want error when body is nil")
	}
}

func TestModelRouterInvalidJSON(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`not json`))
	_, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "valid JSON") {
		t.Errorf("want JSON error, got %v", err)
	}
}

func TestModelRouterMissingModelField(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"messages":[]}`))
	_, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "model") {
		t.Errorf("want missing-model error, got %v", err)
	}
}

func TestModelRouterBodyTooLarge(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	// Pin a small cap so the test stays cheap. Default (32 MiB) would
	// force the test to allocate ~33 MiB.
	const cap = 4096
	r.MaxRoutingBodyBytes = cap
	big := strings.NewReader(strings.Repeat("x", cap+1))
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", big)
	_, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "too large") {
		t.Errorf("want too-large error, got %v", err)
	}
}

// TestModelRouterBodyTooLargeIncludesSize verifies the rejection error
// surfaces the actual body size and the cap, so operators can tell
// from the log how close the client was to the limit.
func TestModelRouterBodyTooLargeIncludesSize(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{}}
	r := NewModelRouter(src)
	const cap = 1024
	r.MaxRoutingBodyBytes = cap
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(strings.Repeat("x", cap+1)))
	_, err := r.Route(req)
	if err == nil {
		t.Fatal("want error, got nil")
	}
	msg := err.Error()
	if !strings.Contains(msg, "too large") {
		t.Errorf("error should mention 'too large'; got %q", msg)
	}
	if !strings.Contains(msg, "cap 1024") {
		t.Errorf("error should mention cap; got %q", msg)
	}
}

// TestModelRouterDefaultCapApplied confirms the router falls back to
// DefaultMaxRoutingBodyBytes when MaxRoutingBodyBytes is left at zero.
func TestModelRouterDefaultCapApplied(t *testing.T) {
	r := NewModelRouter(&fakeSource{m: map[string][]*url.URL{}})
	if got := r.maxJSONBodyBytes(); got != DefaultMaxRoutingBodyBytes {
		t.Errorf("maxJSONBodyBytes = %d; want default %d", got, DefaultMaxRoutingBodyBytes)
	}
}

// TestModelRouterCustomCapHonored verifies a configured MaxRoutingBodyBytes
// overrides the default.
func TestModelRouterCustomCapHonored(t *testing.T) {
	r := NewModelRouter(&fakeSource{m: map[string][]*url.URL{}})
	r.MaxRoutingBodyBytes = 16 << 20
	if got := r.maxJSONBodyBytes(); got != 16<<20 {
		t.Errorf("maxJSONBodyBytes = %d; want %d", got, 16<<20)
	}
}

// TestModelRouterLogsNearCap confirms the router emits a warning when
// the request body crosses nearCapFraction of the configured cap. We
// route the slog handler to a buffer and grep for the warning.
func TestModelRouterLogsNearCap(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a.example:8000")},
	}}
	r := NewModelRouter(src)
	const cap = 1000
	r.MaxRoutingBodyBytes = cap
	var logBuf bytes.Buffer
	r.Logger = slog.New(slog.NewTextHandler(&logBuf, &slog.HandlerOptions{Level: slog.LevelDebug}))

	// 90% of cap — above nearCapFraction (0.8), well below the reject
	// threshold. The leading JSON is padded with trailing whitespace
	// so the total size lands where we want it (JSON tolerates trailing
	// whitespace; json.Unmarshal still parses it).
	body := `{"model":"alpha","messages":[]}`
	padding := cap - len(body)
	if padding <= 0 {
		t.Fatalf("test setup error: cap %d smaller than body %d", cap, len(body))
	}
	bodyBytes := append([]byte(body), bytes.Repeat([]byte{' '}, padding)...)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(bodyBytes))
	if _, err := r.Route(req); err != nil {
		t.Fatalf("Route: %v", err)
	}
	out := logBuf.String()
	if !strings.Contains(out, `msg="routing body near cap"`) {
		t.Errorf("expected near-cap warning in log; got: %s", out)
	}
	if !strings.Contains(out, `cap=1000`) {
		t.Errorf("expected cap in log attrs; got: %s", out)
	}
}

// TestModelRouterQuietUnderNearCap ensures a body well below the
// nearCapFraction does not produce a warning (no spurious noise).
func TestModelRouterQuietUnderNearCap(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a.example:8000")},
	}}
	r := NewModelRouter(src)
	const cap = 10000
	r.MaxRoutingBodyBytes = cap
	var logBuf bytes.Buffer
	r.Logger = slog.New(slog.NewTextHandler(&logBuf, &slog.HandlerOptions{Level: slog.LevelDebug}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"alpha","messages":[]}`))
	if _, err := r.Route(req); err != nil {
		t.Fatalf("Route: %v", err)
	}
	if strings.Contains(logBuf.String(), "routing body near cap") {
		t.Errorf("small body should not log near-cap warning; got: %s", logBuf.String())
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

// TestHandlerRewritesPathAliases verifies that /v1/-prefixed
// endpoints whose backend natively serves at a non-/v1/ path are
// accepted and rewritten before dispatch:
//
//   - /v1/tokenize, /v1/detokenize: llama.cpp and vLLM serve at the
//     bare /tokenize and /detokenize paths.
//   - /v1/file_parse: MinerU's mineru-api serves at the bare /file_parse
//     path.
//
// In all cases external clients use the standard /v1/ surface; we strip
// the prefix so the upstream call lands on the backend's real endpoint.
func TestHandlerRewritesPathAliases(t *testing.T) {
	var seenPath string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, upstream.URL)},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	cases := []struct{ in, want string }{
		{"/v1/tokenize", "/tokenize"},
		{"/v1/detokenize", "/detokenize"},
		{"/v1/file_parse", "/file_parse"},
	}
	for _, c := range cases {
		seenPath = ""
		resp, err := http.Post(proxy.URL+c.in, "application/json",
			strings.NewReader(`{"model":"alpha","content":"hi"}`))
		if err != nil {
			t.Fatalf("%s: %v", c.in, err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("%s: status = %d; want 200", c.in, resp.StatusCode)
		}
		if seenPath != c.want {
			t.Errorf("%s: upstream saw %q; want %q", c.in, seenPath, c.want)
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

// buildAudioMultipart constructs a multipart body that mimics an OpenAI
// /v1/audio/transcriptions request: a "file" file-part with arbitrary
// bytes plus a "model" form-value part.
func buildAudioMultipart(t *testing.T, model string, fileBytes []byte) (body []byte, contentType string) {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)

	fh := make(textproto.MIMEHeader)
	fh.Set("Content-Disposition", `form-data; name="file"; filename="audio.wav"`)
	fh.Set("Content-Type", "audio/wav")
	fp, err := mw.CreatePart(fh)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fp.Write(fileBytes); err != nil {
		t.Fatal(err)
	}

	if err := mw.WriteField("model", model); err != nil {
		t.Fatal(err)
	}
	if err := mw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes(), mw.FormDataContentType()
}

// TestModelRouterRoutesMultipartByModelField verifies that for multipart
// audio requests the router extracts `model` from the form field, not
// the JSON body (which doesn't exist).
func TestModelRouterRoutesMultipartByModelField(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"whisper-1": {mustURL(t, "http://w.example:9000")},
	}}
	r := NewModelRouter(src)

	bodyBytes, ct := buildAudioMultipart(t, "whisper-1", []byte("RIFF\x00\x00\x00\x00WAVEfake-audio"))
	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions",
		bytes.NewReader(bodyBytes))
	req.Header.Set("Content-Type", ct)

	got, err := r.Route(req)
	if err != nil {
		t.Fatalf("Route: %v", err)
	}
	if got.Model != "whisper-1" {
		t.Errorf("Model = %q; want whisper-1", got.Model)
	}
	if len(got.Candidates) != 1 || got.Candidates[0].String() != "http://w.example:9000" {
		t.Errorf("Candidates = %v; want [http://w.example:9000]", got.Candidates)
	}
	if !bytes.Equal(got.Body, bodyBytes) {
		t.Errorf("buffered body does not match original (lengths %d vs %d)", len(got.Body), len(bodyBytes))
	}
}

// TestModelRouterMultipartMissingModel returns a clear error rather than
// trying to JSON-parse a multipart body.
func TestModelRouterMultipartMissingModel(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"whisper-1": {mustURL(t, "http://w.example:9000")},
	}}
	r := NewModelRouter(src)

	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	if err := mw.WriteField("file", "dummy"); err != nil {
		t.Fatal(err)
	}
	mw.Close()

	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions",
		bytes.NewReader(buf.Bytes()))
	req.Header.Set("Content-Type", mw.FormDataContentType())

	_, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "missing model field") {
		t.Errorf("err = %v; want 'missing model field'", err)
	}
}

// TestHandlerForwardsMultipartUnchanged exercises the full handler path:
// a multipart request gets routed by `model` and the upstream sees the
// exact same body bytes plus the same Content-Type boundary.
func TestHandlerForwardsMultipartUnchanged(t *testing.T) {
	var seenBody []byte
	var seenCT, seenPath string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenPath = r.URL.Path
		seenCT = r.Header.Get("Content-Type")
		seenBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"text":"ok"}`))
	}))
	defer upstream.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"whisper-1": {mustURL(t, upstream.URL)},
	}}
	proxy := httptest.NewServer(NewHandler(NewModelRouter(src)))
	defer proxy.Close()

	fileBytes := bytes.Repeat([]byte{0xDE, 0xAD, 0xBE, 0xEF}, 1024)
	bodyBytes, ct := buildAudioMultipart(t, "whisper-1", fileBytes)

	req, _ := http.NewRequest(http.MethodPost, proxy.URL+"/v1/audio/transcriptions",
		bytes.NewReader(bodyBytes))
	req.Header.Set("Content-Type", ct)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d; want 200", resp.StatusCode)
	}
	if seenPath != "/v1/audio/transcriptions" {
		t.Errorf("upstream path = %q; want /v1/audio/transcriptions", seenPath)
	}
	if seenCT != ct {
		t.Errorf("upstream Content-Type = %q; want %q (boundary must round-trip)", seenCT, ct)
	}
	if !bytes.Equal(seenBody, bodyBytes) {
		t.Errorf("upstream body bytes differ (got %d bytes, want %d)", len(seenBody), len(bodyBytes))
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

// TestModelRouterHeaderOverridesBodyModel covers the JSON path: when
// the request has both an X-Model header and a `model` body field with
// conflicting values, the header wins.
func TestModelRouterHeaderOverridesBodyModel(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a.example:8000")},
		"beta":  {mustURL(t, "http://b.example:8000")},
	}}
	r := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"alpha","messages":[]}`))
	req.Header.Set("X-Model", "beta")

	got, err := r.Route(req)
	if err != nil {
		t.Fatalf("Route: %v", err)
	}
	if got.Model != "beta" {
		t.Errorf("Model = %q; want beta (header must override body)", got.Model)
	}
	if len(got.Candidates) != 1 || got.Candidates[0].String() != "http://b.example:8000" {
		t.Errorf("Candidates = %v; want [http://b.example:8000]", got.Candidates)
	}
	if !strings.Contains(string(got.Body), `"model":"alpha"`) {
		t.Errorf("buffered body must keep original (header does not rewrite body); got %q", got.Body)
	}
}

// TestModelRouterHeaderAloneWorksJSON covers the JSON path: when the
// X-Model header is set and the body has no `model` field, the header
// is the sole source of routing truth.
func TestModelRouterHeaderAloneWorksJSON(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://a.example:8000")},
	}}
	r := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"messages":[]}`))
	req.Header.Set("X-Model", "alpha")

	got, err := r.Route(req)
	if err != nil {
		t.Fatalf("Route: %v", err)
	}
	if got.Model != "alpha" {
		t.Errorf("Model = %q; want alpha", got.Model)
	}
}

// TestModelRouterHeaderOverridesFormModel covers the multipart path:
// X-Model header wins over the multipart `model` form field.
func TestModelRouterHeaderOverridesFormModel(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"whisper-1": {mustURL(t, "http://w1.example:9000")},
		"whisper-2": {mustURL(t, "http://w2.example:9000")},
	}}
	r := NewModelRouter(src)

	bodyBytes, ct := buildAudioMultipart(t, "whisper-1", []byte("RIFFfake"))
	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions",
		bytes.NewReader(bodyBytes))
	req.Header.Set("Content-Type", ct)
	req.Header.Set("X-Model", "whisper-2")

	got, err := r.Route(req)
	if err != nil {
		t.Fatalf("Route: %v", err)
	}
	if got.Model != "whisper-2" {
		t.Errorf("Model = %q; want whisper-2 (header must override form field)", got.Model)
	}
	if len(got.Candidates) != 1 || got.Candidates[0].String() != "http://w2.example:9000" {
		t.Errorf("Candidates = %v; want [http://w2.example:9000]", got.Candidates)
	}
}

// TestModelRouterHeaderAloneWorksMultipart covers the multipart path:
// X-Model header is set and the multipart body has no `model` form
// field. The body must still be buffered for upstream replay.
func TestModelRouterHeaderAloneWorksMultipart(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"whisper-1": {mustURL(t, "http://w.example:9000")},
	}}
	r := NewModelRouter(src)

	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fh := make(textproto.MIMEHeader)
	fh.Set("Content-Disposition", `form-data; name="file"; filename="audio.wav"`)
	fh.Set("Content-Type", "audio/wav")
	fp, err := mw.CreatePart(fh)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fp.Write([]byte("RIFFfake")); err != nil {
		t.Fatal(err)
	}
	if err := mw.Close(); err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions",
		bytes.NewReader(buf.Bytes()))
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("X-Model", "whisper-1")

	got, err := r.Route(req)
	if err != nil {
		t.Fatalf("Route: %v", err)
	}
	if got.Model != "whisper-1" {
		t.Errorf("Model = %q; want whisper-1", got.Model)
	}
	if !bytes.Equal(got.Body, buf.Bytes()) {
		t.Errorf("buffered body bytes differ (got %d, want %d)", len(got.Body), buf.Len())
	}
}

// TestModelRouterUnknownModelFromHeader ensures the unknown-model
// error path works the same way when the model comes from the header.
func TestModelRouterUnknownModelFromHeader(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{"alpha": {mustURL(t, "http://a:8000")}}}
	r := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"messages":[]}`))
	req.Header.Set("X-Model", "ghost")

	_, err := r.Route(req)
	if err == nil || !strings.Contains(err.Error(), "unknown model") {
		t.Errorf("want unknown model error, got %v", err)
	}
}
