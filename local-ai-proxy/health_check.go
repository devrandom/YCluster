package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
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
	exoCatalog := false
	if err == nil {
		var parsed struct {
			Data []struct {
				ID      string `json:"id"`
				OwnedBy string `json:"owned_by"`
			} `json:"data"`
		}
		if json.Unmarshal(body, &parsed) == nil && parsed.Data != nil {
			models = make([]string, 0, len(parsed.Data))
			for _, m := range parsed.Data {
				models = append(models, m.ID)
				if m.OwnedBy == "exo" {
					exoCatalog = true
				}
			}
		}
	}

	// exo's /v1/models is a catalog of every known model card, not a
	// placement report: it stays 200 with no instance placed. Ask the
	// placement state instead, so an idle backend is not reported healthy.
	if exoCatalog {
		hc.checkExo(ctx, u)
		return
	}
	hc.record(u, StateHealthy, "", models)
}

// checkExo treats an exo backend as healthy only when at least one
// placed instance has every runner in RunnerReady or RunnerRunning
// (exo dispatches work in both states). Models is set to the ready
// instances' model IDs, which also repairs ServesModel for a backend
// whose /v1/models lists the whole catalog.
//
// The state responses are sub-objects of /state (the full document can
// grow to tens of megabytes), and both are small in normal operation.
func (hc *HealthChecker) checkExo(ctx context.Context, u *url.URL) {
	var instances map[string]json.RawMessage
	if err := hc.fetchState(ctx, u, "/state/instances", &instances, 1<<20); err != nil {
		if ctx.Err() != nil {
			return // shutting down
		}
		hc.record(u, StateDown, "exo instances: "+err.Error(), []string{})
		return
	}
	var runners map[string]json.RawMessage
	if err := hc.fetchState(ctx, u, "/state/runners", &runners, 1<<20); err != nil {
		if ctx.Err() != nil {
			return // shutting down
		}
		hc.record(u, StateDown, "exo runners: "+err.Error(), []string{})
		return
	}

	ready := make(map[string]bool, len(runners))
	for id, raw := range runners {
		var state map[string]json.RawMessage
		if json.Unmarshal(raw, &state) != nil {
			continue
		}
		// exo's planner dispatches to runners in RunnerReady or
		// RunnerRunning; the latter is the steady state while a
		// generation is in flight, so treating it as down would flap
		// the backend under load.
		_, readyOK := state["RunnerReady"]
		_, runningOK := state["RunnerRunning"]
		ready[id] = readyOK || runningOK
	}

	if len(instances) == 0 {
		hc.record(u, StateDown, "no instance placed", []string{})
		return
	}

	models := make([]string, 0, len(instances))
	seen := make(map[string]bool, len(instances))
	sawInstance := false
	for _, raw := range instances {
		var wrapper map[string]json.RawMessage
		if json.Unmarshal(raw, &wrapper) != nil {
			continue
		}
		for _, meta := range wrapper {
			var inst struct {
				ShardAssignments struct {
					ModelID       string         `json:"modelId"`
					RunnerToShard map[string]any `json:"runnerToShard"`
				} `json:"shardAssignments"`
			}
			if json.Unmarshal(meta, &inst) != nil || inst.ShardAssignments.ModelID == "" {
				continue
			}
			sawInstance = true
			if len(inst.ShardAssignments.RunnerToShard) == 0 {
				continue
			}
			allReady := true
			for runnerID := range inst.ShardAssignments.RunnerToShard {
				if !ready[runnerID] {
					allReady = false
					break
				}
			}
			if !allReady {
				continue
			}
			if !seen[inst.ShardAssignments.ModelID] {
				seen[inst.ShardAssignments.ModelID] = true
				models = append(models, inst.ShardAssignments.ModelID)
			}
		}
	}

	if !sawInstance {
		hc.record(u, StateDown, "exo instances: unrecognized state", []string{})
		return
	}
	if len(models) == 0 {
		hc.record(u, StateDown, "runners not ready", models)
		return
	}
	sort.Strings(models)
	hc.record(u, StateHealthy, "", models)
}

// fetchState GETs and decodes a JSON sub-object from the backend,
// capping the response body at maxBytes.
func (hc *HealthChecker) fetchState(ctx context.Context, u *url.URL, path string, v any, maxBytes int64) error {
	checkURL := *u
	checkURL.Path = joinPath(checkURL.Path, path)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, checkURL.String(), nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	resp, err := hc.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxBytes))
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
	if err != nil {
		return err
	}
	if int64(len(body)) > maxBytes {
		return fmt.Errorf("%s: response exceeds %d bytes", path, maxBytes)
	}
	if err := json.Unmarshal(body, v); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	return nil
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
