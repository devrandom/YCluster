package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"sort"
)

// ErrNoHealthyBackend is returned when every backend registered for a
// model is currently unhealthy (down, disabled, or unknown). The
// handler converts this into a 503 rather than a 400.
var ErrNoHealthyBackend = errors.New("no healthy backend for model")

// Healthy reports whether a backend URL is currently known-healthy.
// Used by ModelRouter to filter candidate backends. A nil Healthy
// means "trust the source list as-is" (used in tests / passthrough).
type Healthy interface {
	IsHealthy(urlStr string) bool
}

// maxRoutingBodyBytes caps how much of a request body we'll read to find
// the model field. Anything larger is rejected.
const maxRoutingBodyBytes = 8 << 20 // 8 MiB

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
}

func NewModelRouter(source Source) *ModelRouter {
	return &ModelRouter{source: source}
}

func (m *ModelRouter) Route(r *http.Request) (*RouteResult, error) {
	if r.Body == nil {
		return nil, errors.New("request has no body; cannot determine model")
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxRoutingBodyBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read request body: %w", err)
	}
	if len(body) > maxRoutingBodyBytes {
		return nil, errors.New("request body too large")
	}

	var env struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, fmt.Errorf("request body is not valid JSON: %w", err)
	}
	if env.Model == "" {
		return nil, errors.New("request body missing model field")
	}

	urls, ok := m.source.Snapshot()[env.Model]
	if !ok || len(urls) == 0 {
		return nil, fmt.Errorf("unknown model: %s", env.Model)
	}

	candidates := urls
	if m.Healthy != nil {
		filtered := make([]*url.URL, 0, len(urls))
		for _, u := range urls {
			if m.Healthy.IsHealthy(u.String()) {
				filtered = append(filtered, u)
			}
		}
		if len(filtered) == 0 {
			return nil, fmt.Errorf("%w: %s", ErrNoHealthyBackend, env.Model)
		}
		candidates = filtered
	}

	return &RouteResult{Model: env.Model, Candidates: candidates, Body: body, Stream: env.Stream}, nil
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
