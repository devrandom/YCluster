package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
)

// maxRoutingBodyBytes caps how much of a request body we'll read to find
// the model field. Anything larger is rejected.
const maxRoutingBodyBytes = 8 << 20 // 8 MiB

// Router decides which backend to forward a request to.
type Router interface {
	// Route returns the backend URL for r. If Route consumed r.Body to
	// find the model name, it returns a substitute body that the caller
	// must use in place of r.Body; otherwise it returns nil.
	Route(r *http.Request) (*url.URL, io.Reader, error)

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

func (p *PassthroughRouter) Route(r *http.Request) (*url.URL, io.Reader, error) {
	return p.backend, nil, nil
}

func (p *PassthroughRouter) Models() []string { return nil }

// ModelRouter routes requests by the "model" field in the JSON body.
type ModelRouter struct {
	source Source
}

func NewModelRouter(source Source) *ModelRouter {
	return &ModelRouter{source: source}
}

func (m *ModelRouter) Route(r *http.Request) (*url.URL, io.Reader, error) {
	if r.Body == nil {
		return nil, nil, errors.New("request has no body; cannot determine model")
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxRoutingBodyBytes+1))
	if err != nil {
		return nil, nil, fmt.Errorf("read request body: %w", err)
	}
	if len(body) > maxRoutingBodyBytes {
		return nil, nil, errors.New("request body too large")
	}

	var env struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, nil, fmt.Errorf("request body is not valid JSON: %w", err)
	}
	if env.Model == "" {
		return nil, nil, errors.New("request body missing model field")
	}

	urls, ok := m.source.Snapshot()[env.Model]
	if !ok || len(urls) == 0 {
		return nil, nil, fmt.Errorf("unknown model: %s", env.Model)
	}
	// Multi-backend fan-out is a TODO; today we pick the first.
	return urls[0], bytes.NewReader(body), nil
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
