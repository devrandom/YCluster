package main

import (
	"encoding/json"
	"net/http"
	"net/url"
	"sort"
	"time"
)

type healthzBackend struct {
	URL       string   `json:"url"`
	State     string   `json:"state"`
	LastCheck string   `json:"last_check"`
	Err       string   `json:"err,omitempty"`
	Models    []string `json:"models,omitempty"`
}

type healthzModel struct {
	Name     string   `json:"name"`
	State    string   `json:"state"`
	Backends []string `json:"backends"`
}

func writeHealthz(w http.ResponseWriter, r *http.Request, snap map[string]BackendHealth, models map[string][]*url.URL, auth *authState) {
	backends, healthy, down, disabled := healthzBackendEntries(snap)
	modelEntries, modelHealthy, _, modelUnavailable := healthzModelEntries(models, snap)
	overall := healthzOverall(modelEntries, modelHealthy, modelUnavailable)

	resp := struct {
		Status   string           `json:"status"`
		Healthy  int              `json:"healthy"`
		Down     int              `json:"down"`
		Disabled int              `json:"disabled"`
		Backends []healthzBackend `json:"backends"`
		Models   []healthzModel   `json:"models"`
		Auth     *authState       `json:"auth,omitempty"`
	}{
		Status:   overall,
		Healthy:  healthy,
		Down:     down,
		Disabled: disabled,
		Backends: backends,
		Models:   modelEntries,
	}

	status := http.StatusOK
	if auth != nil {
		resp.Auth = auth
		if auth.Status != "ok" {
			resp.Status = "down"
			status = http.StatusServiceUnavailable
		}
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(resp)
}

// healthzBackendEntries renders sorted backend rows and simple state counts.
func healthzBackendEntries(snap map[string]BackendHealth) ([]healthzBackend, int, int, int) {
	urls := make([]string, 0, len(snap))
	for k := range snap {
		urls = append(urls, k)
	}
	sort.Strings(urls)

	backends := make([]healthzBackend, 0, len(urls))
	healthy, down, disabled := 0, 0, 0
	for _, u := range urls {
		bh := snap[u]
		switch bh.State {
		case StateHealthy:
			healthy++
		case StateDown:
			down++
		case StateDisabled:
			disabled++
		}
		backend := healthzBackend{
			URL:       u,
			State:     bh.State.String(),
			LastCheck: bh.LastCheck.UTC().Format(time.RFC3339),
			Models:    bh.Models,
		}
		if bh.Err != "" {
			backend.Err = bh.Err
		}
		backends = append(backends, backend)
	}
	return backends, healthy, down, disabled
}

// healthzModelEntries renders sorted model rollups and counts accountable
// states separately from disabled models.
func healthzModelEntries(models map[string][]*url.URL, snap map[string]BackendHealth) ([]healthzModel, int, int, int) {
	modelNames := make([]string, 0, len(models))
	for name := range models {
		modelNames = append(modelNames, name)
	}
	sort.Strings(modelNames)

	modelEntries := make([]healthzModel, 0, len(modelNames))
	healthy, disabled, unavailable := 0, 0, 0
	for _, name := range modelNames {
		urls := models[name]
		urlStrs := make([]string, 0, len(urls))
		for _, u := range urls {
			urlStrs = append(urlStrs, u.String())
		}
		state := modelStateFromBackends(urls, snap)
		switch state {
		case "healthy":
			healthy++
		case "disabled":
			disabled++
		case "unavailable":
			unavailable++
		}
		modelEntries = append(modelEntries, healthzModel{
			Name: name, State: state, Backends: urlStrs,
		})
	}
	return modelEntries, healthy, disabled, unavailable
}

// healthzOverall rolls model state up to the /healthz status. A down backend
// with healthy siblings for its model does not page; disabled models are
// operator-acknowledged and do not count as alerts.
func healthzOverall(models []healthzModel, healthy, unavailable int) string {
	accountable := healthy + unavailable
	switch {
	case len(models) == 0:
		return "unknown"
	case healthy == 0 && unavailable == 0:
		return "unknown"
	case unavailable > 0 && healthy == 0 && accountable > 0:
		return "down"
	case unavailable > 0:
		return "degraded"
	default:
		return "ok"
	}
}
