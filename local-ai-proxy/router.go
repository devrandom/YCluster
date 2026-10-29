package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"mime"
	"mime/multipart"
	"net/http"
	"net/url"
	"sort"
	"strings"
)

// ErrNoHealthyBackend is returned when every backend registered for a
// model is currently unhealthy (down, disabled, or unknown). The
// handler converts this into a 503 rather than a 400.
var ErrNoHealthyBackend = errors.New("no healthy backend for model")

// Healthy reports whether a backend URL is currently known-healthy
// and whether it serves a given model. Used by ModelRouter to filter
// candidate backends. A nil Healthy means "trust the source list
// as-is" (used in tests / passthrough).
type Healthy interface {
	IsHealthy(urlStr string) bool
	ServesModel(urlStr, model string) bool
}

// DefaultMaxRoutingBodyBytes is the JSON request-body cap used when
// Config.MaxRoutingBodyBytes is unset or non-positive. Sized for
// base64-encoded multimodal chat completions: a handful of images
// plus a prompt envelope blow past 8 MiB, so 32 MiB is the practical
// floor. Raise via Config if you serve heavier payloads.
const DefaultMaxRoutingBodyBytes = 32 << 20 // 32 MiB

// nearCapFraction is the body-size ratio (relative to the cap) at
// which the router logs a warning. The router buffers up to the cap
// to find the `model` field, so warnings mean "this client is close
// to hitting the limit; consider raising it."
const nearCapFraction = 0.8

// maxMultipartBodyBytes caps how much of a multipart request body we'll
// buffer. Multipart is used by OpenAI's audio endpoints, where the body
// is a binary file upload plus a small `model` form field. Sized for
// roughly a 5-minute 24 kHz/16-bit WAV with overhead; raise via
// MaxMultipartBodyBytes if you need bigger uploads.
const maxMultipartBodyBytes = 200 << 20 // 200 MiB

// maxFormFieldBytes caps how many bytes of a single form field's value
// we'll read. The `model` field is always tiny; this prevents a
// malicious client from forcing us to buffer a huge field while
// scanning for it.
const maxFormFieldBytes = 4096

// RouteResult is what a Router returns for a single request.
//
// Model is the routed model name, or "" in passthrough mode where the
// router doesn't inspect the body. Candidates are the backend URLs
// eligible to serve the request, ordered arbitrarily; the caller
// picks one (typically least-loaded) and may retry through the rest
// on failure. Body is the buffered request body that fan-out retries
// replay; nil means "use r.Body" (single-attempt only, passthrough).
// Stream mirrors the request body's "stream" field — used as a
// metric label so TTFT can be interpreted correctly.
type RouteResult struct {
	Model      string
	Candidates []*url.URL
	Body       []byte
	Stream     bool
}

// Router decides which backends are eligible to serve a request.
type Router interface {
	Route(r *http.Request) (*RouteResult, error)

	// Models returns the set of known models, or nil if the router does
	// not know (caller should proxy /v1/models upstream instead).
	Models() []string
}

// PassthroughRouter always returns the same backend URL. Used when the
// proxy has a single upstream and doesn't need to inspect request bodies.
type PassthroughRouter struct {
	backend *url.URL
}

func NewPassthroughRouter(backend *url.URL) *PassthroughRouter {
	return &PassthroughRouter{backend: backend}
}

func (p *PassthroughRouter) Route(r *http.Request) (*RouteResult, error) {
	return &RouteResult{Candidates: []*url.URL{p.backend}}, nil
}

func (p *PassthroughRouter) Models() []string { return nil }

// ModelRouter routes requests by the "model" field in the JSON body.
// When multiple backends are registered for a model, ModelRouter picks
// the least-loaded healthy one (ties broken randomly).
type ModelRouter struct {
	source Source

	// Healthy, if set, filters out backends that are not currently
	// known-healthy. Leave nil to skip health filtering.
	Healthy Healthy

	// Load, if set, is consulted to pick the least-loaded candidate
	// among healthy backends. Leave nil to pick randomly.
	Load Load

	// MaxRoutingBodyBytes caps how much of a JSON request body we'll
	// read to find the model field. Anything larger is rejected.
	// Zero or negative falls back to DefaultMaxRoutingBodyBytes.
	// Set from Config.MaxRoutingBodyBytes by the caller (main.go).
	MaxRoutingBodyBytes int64

	// Logger receives the "routing body near cap" warning. Nil-safe
	// (slog.Default when unset).
	Logger *slog.Logger
}

// SupersessionSource is an optional Source extension exposing a
// hot-reloaded obsolete → replacement model map. EtcdSource implements
// it; sources without supersession simply don't.
type SupersessionSource interface {
	SupersededSnapshot() map[string]string
}

func NewModelRouter(source Source) *ModelRouter {
	return &ModelRouter{source: source}
}

// maxJSONBodyBytes returns the effective JSON body cap, falling back
// to DefaultMaxRoutingBodyBytes when the field is unset.
func (m *ModelRouter) maxJSONBodyBytes() int64 {
	if m.MaxRoutingBodyBytes > 0 {
		return m.MaxRoutingBodyBytes
	}
	return DefaultMaxRoutingBodyBytes
}

// logger returns the configured logger or slog.Default when unset.
func (m *ModelRouter) logger() *slog.Logger {
	if m.Logger != nil {
		return m.Logger
	}
	return slog.Default()
}

func (m *ModelRouter) Route(r *http.Request) (*RouteResult, error) {
	if r.Body == nil {
		return nil, errors.New("request has no body; cannot determine model")
	}

	// X-Model header overrides any model field in the body. Useful when
	// the client can't easily embed `model` in the body (e.g. raw curl
	// uploads, or clients that want to keep one model value pinned in
	// headers while the body varies). Body is still buffered (for
	// fan-out retry replay) but its `model` field is ignored.
	headerModel := strings.TrimSpace(r.Header.Get("X-Model"))

	// Branch by Content-Type. Multipart bodies (OpenAI audio endpoints)
	// can't be JSON-parsed; we parse the form just enough to find the
	// `model` field and then replay the original bytes upstream.
	ct := r.Header.Get("Content-Type")
	if mediaType, params, err := mime.ParseMediaType(ct); err == nil &&
		strings.HasPrefix(mediaType, "multipart/") {
		return m.routeMultipart(r, params["boundary"], headerModel)
	}
	return m.routeJSON(r, headerModel)
}

func (m *ModelRouter) routeJSON(r *http.Request, headerModel string) (*RouteResult, error) {
	maxBytes := m.maxJSONBodyBytes()
	// Read cap+1 so a body that exactly hits the cap is still accepted;
	// only `len(body) > maxBytes` triggers the reject (the +1 byte is
	// the tell).
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read request body: %w", err)
	}
	if int64(len(body)) > maxBytes {
		return nil, fmt.Errorf("request body too large: %d bytes (cap %d)", len(body), maxBytes)
	}
	if int64(len(body)) >= int64(float64(maxBytes)*nearCapFraction) {
		m.logger().Warn(
			"routing body near cap",
			"bytes", len(body),
			"cap", maxBytes,
			"method", r.Method,
			"path", r.URL.Path,
		)
	}

	var env struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, fmt.Errorf("request body is not valid JSON: %w", err)
	}

	model := headerModel
	if model == "" {
		if env.Model == "" {
			return nil, errors.New("request body missing model field")
		}
		model = env.Model
	}

	if err := m.checkSuperseded(model); err != nil {
		return nil, err
	}

	candidates, err := m.candidatesFor(model)
	if err != nil {
		return nil, err
	}
	return &RouteResult{Model: model, Candidates: candidates, Body: body, Stream: env.Stream}, nil
}

// routeMultipart reads the request body once, locates the `model` form
// field by scanning multipart parts in order, and returns the original
// raw body for replay. The file part is never decoded — we read it as
// opaque bytes so binary uploads pass through unchanged.
func (m *ModelRouter) routeMultipart(r *http.Request, boundary string, headerModel string) (*RouteResult, error) {
	if boundary == "" {
		return nil, errors.New("multipart Content-Type missing boundary")
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxMultipartBodyBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read request body: %w", err)
	}
	if len(body) > maxMultipartBodyBytes {
		return nil, errors.New("request body too large")
	}

	model := headerModel
	if model == "" {
		model, err = extractMultipartModel(body, boundary)
		if err != nil {
			return nil, err
		}
	}

	if model == "" {
		return nil, errors.New("multipart body missing model field")
	}

	if err := m.checkSuperseded(model); err != nil {
		return nil, err
	}

	candidates, err := m.candidatesFor(model)
	if err != nil {
		return nil, err
	}
	// Multipart audio endpoints don't stream the way chat completions do;
	// leave Stream=false (it's only used as a metric label).
	return &RouteResult{Model: model, Candidates: candidates, Body: body, Stream: false}, nil
}

// extractMultipartModel scans the multipart body for the `model` form field
// and returns its trimmed string value. The file part is never decoded — we
// read it as opaque bytes so binary uploads pass through unchanged.
func extractMultipartModel(body []byte, boundary string) (string, error) {
	mr := multipart.NewReader(bytes.NewReader(body), boundary)
	for {
		part, err := mr.NextRawPart()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return "", fmt.Errorf("parse multipart body: %w", err)
		}
		if part.FormName() == "model" && part.FileName() == "" {
			val, readErr := io.ReadAll(io.LimitReader(part, maxFormFieldBytes+1))
			part.Close()
			if readErr != nil {
				return "", fmt.Errorf("read model form field: %w", readErr)
			}
			if len(val) > maxFormFieldBytes {
				return "", errors.New("model form field too large")
			}
			return strings.TrimSpace(string(val)), nil
		}
		part.Close()
	}
	return "", nil
}

// checkSuperseded rejects obsolete model names before any routing or
// ACL work, so superseded requests never reach a backend. Sources
// without supersession (e.g. YAMLSource) never reject.
func (m *ModelRouter) checkSuperseded(model string) error {
	if ss, ok := m.source.(SupersessionSource); ok {
		if replacement, ok := ss.SupersededSnapshot()[model]; ok {
			return &SupersededError{Model: model, SupersededBy: replacement}
		}
	}
	return nil
}

// candidatesFor looks up backends for a model and filters by health.
func (m *ModelRouter) candidatesFor(model string) ([]*url.URL, error) {
	urls, ok := m.source.Snapshot()[model]
	if !ok || len(urls) == 0 {
		return nil, fmt.Errorf("unknown model: %s", model)
	}
	if m.Healthy == nil {
		return urls, nil
	}
	filtered := make([]*url.URL, 0, len(urls))
	for _, u := range urls {
		if m.Healthy.IsHealthy(u.String()) && m.Healthy.ServesModel(u.String(), model) {
			filtered = append(filtered, u)
		}
	}
	if len(filtered) == 0 {
		return nil, fmt.Errorf("%w: %s", ErrNoHealthyBackend, model)
	}
	return filtered, nil
}

// PickBackend selects one URL from candidates. With a Load, picks the
// lowest in-flight count (random tie-break). Without, picks uniformly
// at random. candidates must be non-empty.
//
// Counts are sampled once up front so concurrent Inc/Dec can't leave
// the tie-set empty between passes.
func PickBackend(candidates []*url.URL, load Load) *url.URL {
	if len(candidates) == 1 {
		return candidates[0]
	}
	if load == nil {
		return candidates[rand.IntN(len(candidates))]
	}
	counts := make([]int64, len(candidates))
	minCount := int64(-1)
	for i, u := range candidates {
		counts[i] = load.Count(u.String())
		if minCount < 0 || counts[i] < minCount {
			minCount = counts[i]
		}
	}
	tied := make([]*url.URL, 0, len(candidates))
	for i, u := range candidates {
		if counts[i] == minCount {
			tied = append(tied, u)
		}
	}
	return tied[rand.IntN(len(tied))]
}

func (m *ModelRouter) Models() []string {
	snap := m.source.Snapshot()
	out := make([]string, 0, len(snap))
	for k := range snap {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
