package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"
)

func (hc *HealthChecker) checkOne(ctx context.Context, u *url.URL) {
	checkURL := *u
	checkURL.Path = joinPath(checkURL.Path, "/v1/models")

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, checkURL.String(), nil)
	if err != nil {
		hc.record(u, StateDown, fmt.Sprintf("build request: %v", err), nil)
		return
	}
	resp, err := hc.client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return // shutting down
		}
		hc.record(u, StateDown, err.Error(), nil)
		return
	}
	defer resp.Body.Close()

	// Treat any HTTP response as "backend is reachable" — a 4xx from
	// auth or a missing endpoint still means the server is up. 5xx is
	// counted as down.
	if resp.StatusCode >= 500 {
		hc.record(u, StateDown, fmt.Sprintf("HTTP %d", resp.StatusCode), nil)
		return
	}

	// Parse the /v1/models response to learn which model IDs the backend
	// serves. Cap the body at 1 MiB — backends like exo expose ~120 models
	// but no /v1/models response should be megabytes. If the body isn't
	// valid JSON or doesn't match the OpenAI shape, leave Models nil so
	// ServesModel returns true (don't filter a reachable backend we
	// couldn't interrogate).
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var models []string
	if err == nil {
		var parsed struct {
			Data []struct {
				ID string `json:"id"`
			} `json:"data"`
		}
		if json.Unmarshal(body, &parsed) == nil && parsed.Data != nil {
			models = make([]string, 0, len(parsed.Data))
			for _, m := range parsed.Data {
				models = append(models, m.ID)
			}
		}
	}
	hc.record(u, StateHealthy, "", models)
}

func (hc *HealthChecker) record(u *url.URL, state BackendState, errMsg string, models []string) {
	hc.mu.Lock()
	prev, had := hc.states[u.String()]
	next := BackendHealth{
		URL:       u,
		State:     state,
		LastCheck: time.Now(),
		Err:       errMsg,
		Models:    models,
	}
	hc.states[u.String()] = next
	hc.mu.Unlock()

	hc.Metrics.SetBackendState(u.String(), state.String())
	hc.updateModelMetrics()

	if !had || prev.State != state {
		msg := "backend healthy"
		lvl := slog.LevelInfo
		switch state {
		case StateDown:
			msg = "backend down"
			lvl = slog.LevelWarn
		case StateDisabled:
			msg = "backend disabled"
			lvl = slog.LevelInfo
		}
		if errMsg != "" {
			hc.logger.Log(context.Background(), lvl, msg,
				"backend", u.String(), "err", errMsg)
		} else {
			hc.logger.Log(context.Background(), lvl, msg, "backend", u.String())
		}
	}
}
