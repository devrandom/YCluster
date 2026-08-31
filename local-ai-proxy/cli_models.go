package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
)

func modelsAdd(args []string, configPath string) {
	fs := flag.NewFlagSet("models add", flag.ExitOnError)
	fs.Usage = func() { fmt.Fprintln(os.Stderr, "usage: local-ai-proxy models add <api-base> [model]") }
	_ = fs.Parse(args)

	apiBase, model, err := parseModelsAddArgs(fs.Args(), fs)
	if err != nil {
		fatal("%v", err)
	}
	client, prefix := mustEtcdForCLI(configPath)
	defer client.Close()
	if model != "" {
		modelsAddOne(client, prefix, model, apiBase)
		return
	}
	modelsAddDiscovered(client, prefix, apiBase)
}

// parseModelsAddArgs validates the positional arguments accepted by
// `models add <api-base> [model]`. An empty model means "discover models".
func parseModelsAddArgs(rest []string, fs *flag.FlagSet) (apiBase, model string, err error) {
	if len(rest) == 0 || len(rest) > 2 {
		fs.Usage()
		return "", "", errors.New("models add takes <api-base> [model]")
	}
	apiBase, err = normalizeBackendURL(rest[0])
	if err != nil {
		return "", "", err
	}
	if len(rest) == 2 {
		model = rest[1]
		if err := validateModelName(model); err != nil {
			return "", "", err
		}
	}
	return apiBase, model, nil
}

// modelsAddDiscovered discovers models from the backend and adds them all.
func modelsAddDiscovered(client *clientv3.Client, prefix, apiBase string) {
	ids, err := discoverModels(apiBase)
	if err != nil {
		fatal("%v", err)
	}
	if len(ids) == 0 {
		fatal("backend at %s returned no models", apiBase)
	}
	added, skipped := addDiscoveredModels(client, prefix, apiBase, ids)
	fmt.Printf("\nAdded %d, skipped %d.\n", added, skipped)
}

// addDiscoveredModels upserts each discovered model, treating invalid names
// and already-configured backends as skips.
func addDiscoveredModels(client *clientv3.Client, prefix, apiBase string, ids []string) (added, skipped int) {
	for _, id := range ids {
		if err := validateModelName(id); err != nil {
			fmt.Printf("  skip:  %s (%v)\n", id, err)
			skipped++
			continue
		}
		if modelsAddOneResult(client, prefix, id, apiBase) {
			fmt.Printf("  added: %s -> %s\n", id, apiBase)
			added++
		} else {
			fmt.Printf("  skip:  %s (already configured for %s)\n", id, apiBase)
			skipped++
		}
	}
	return added, skipped
}

func modelsAddOne(client *clientv3.Client, prefix, model, apiBase string) {
	if modelsAddOneResult(client, prefix, model, apiBase) {
		fmt.Printf("Added: %s -> %s\n", model, apiBase)
	} else {
		fmt.Printf("Already configured: %s -> %s\n", model, apiBase)
	}
}

// modelsAddOneResult upserts a (model, api_base) mapping. Returns true
// if it wrote a change, false if that exact backend was already present.
func modelsAddOneResult(client *clientv3.Client, prefix, model, apiBase string) bool {
	key := prefix + model
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	v := getEtcdModelValue(ctx, client, key)
	if backendExists(v.Backends, apiBase) {
		return false
	}
	v.Backends = append(v.Backends, etcdBackendEntry{APIBase: apiBase})
	putEtcdModelValue(ctx, client, key, v)
	return true
}

func modelsRemove(args []string, configPath string) {
	fs := flag.NewFlagSet("models remove", flag.ExitOnError)
	apiBaseFlag := fs.String("api-base", "", "backend to remove (alternative to giving the URL positionally)")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: local-ai-proxy models remove <model> [--api-base <url>]")
		fmt.Fprintln(os.Stderr, "       local-ai-proxy models remove <url> [model]")
		fmt.Fprintln(os.Stderr, "a positional containing '://' is treated as a backend URL;")
		fmt.Fprintln(os.Stderr, "<url> alone removes that backend from every model.")
	}
	_ = fs.Parse(args)

	model, apiBase := resolveRemoveArgs(fs.Args(), *apiBaseFlag, fs)
	if model == "" && apiBase == "" {
		os.Exit(2)
	}

	client, prefix := mustEtcdForCLI(configPath)
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	modelsRemoveTarget(ctx, client, prefix, model, apiBase)
}

// modelsRemoveTarget dispatches to the three removal modes: remove a backend
// from every model, delete an entire model, or remove one backend from one
// model.
func modelsRemoveTarget(ctx context.Context, client *clientv3.Client, prefix, model, apiBase string) {
	if model == "" {
		modelsRemoveBackendEverywhere(ctx, client, prefix, apiBase)
		return
	}
	if apiBase == "" {
		deleteEtcdKey(ctx, client, prefix+model)
		fmt.Printf("Removed model: %s\n", model)
		return
	}
	modelsRemoveOne(ctx, client, prefix, model, apiBase)
}

// resolveRemoveArgs accepts both the model-first and URL-first forms:
// `<model> [--api-base <url>]` and `<url> [model]`. A positional containing
// "://" is treated as a backend URL; the other as the model name.
func resolveRemoveArgs(rest []string, apiBase string, fs *flag.FlagSet) (model, resolvedURL string) {
	resolvedURL = apiBase
	for _, a := range rest {
		if strings.Contains(a, "://") {
			if resolvedURL != "" && resolvedURL != a {
				fatal("backend specified twice (%s and %s)", resolvedURL, a)
			}
			resolvedURL = a
		} else {
			if model != "" {
				fatal("more than one model given (%s and %s)", model, a)
			}
			model = a
		}
	}
	if model == "" && resolvedURL == "" {
		fs.Usage()
	}
	return model, resolvedURL
}

// modelsRemoveOne removes a single backend from a single model. When the
// removed backend is the last, the model is deleted entirely.
func modelsRemoveOne(ctx context.Context, client *clientv3.Client, prefix, model, rawAPIBase string) {
	key := prefix + model
	v := mustGetEtcdModelValue(client, key, model)
	target, err := normalizeBackendURL(rawAPIBase)
	if err != nil {
		fatal("%v", err)
	}

	filtered, removed := filterBackends(v.Backends, target)
	if !removed {
		fatal("model %s has no backend %s", model, target)
	}
	if len(filtered) == 0 {
		deleteEtcdKey(ctx, client, key)
		fmt.Printf("Removed last backend from %s — model deleted.\n", model)
		return
	}
	v.Backends = filtered
	mustPutEtcdModelValue(client, key, v)
	fmt.Printf("Removed backend %s from %s.\n", target, model)
}

// filterBackends returns the backend slice without target, using the same
// in-place filter semantics as the rest of the CLI. Returns the filtered
// slice and whether the target was present.
func filterBackends(backends []etcdBackendEntry, target string) ([]etcdBackendEntry, bool) {
	filtered := backends[:0]
	removed := false
	for _, b := range backends {
		if b.APIBase == target {
			removed = true
			continue
		}
		filtered = append(filtered, b)
	}
	return filtered, removed
}

func deleteEtcdKey(ctx context.Context, client *clientv3.Client, key string) {
	if _, err := client.Delete(ctx, key); err != nil {
		fatal("etcd delete %s: %v", key, err)
	}
}

// modelsRemoveBackendEverywhere drops the given backend URL from every
// model that references it. Models left with no backends are deleted.
func modelsRemoveBackendEverywhere(ctx context.Context, client *clientv3.Client, prefix, apiBase string) {
	target, err := normalizeBackendURL(apiBase)
	if err != nil {
		fatal("%v", err)
	}
	resp, err := client.Get(ctx, prefix, clientv3.WithPrefix())
	if err != nil {
		fatal("etcd get %s: %v", prefix, err)
	}

	touched, deleted := countBackendRemovals(ctx, client, prefix, resp.Kvs, target)
	if touched == 0 {
		fmt.Printf("No models reference backend %s\n", target)
		return
	}
	fmt.Printf("Removed backend %s from %d model(s); %d model(s) deleted.\n", target, touched, deleted)
}

// countBackendRemovals filters target from each model under prefix.
// Returns the number of models updated and deleted.
func countBackendRemovals(ctx context.Context, client *clientv3.Client, prefix string, kvs []*mvccpb.KeyValue, target string) (touched, deleted int) {
	for _, kv := range kvs {
		name := strings.TrimPrefix(string(kv.Key), prefix)
		updated, modelDeleted, ok := removeBackendFromValue(ctx, client, string(kv.Key), name, kv.Value, target)
		if !ok {
			continue
		}
		touched += boolToInt(updated)
		deleted += boolToInt(modelDeleted)
	}
	return touched, deleted
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// removeBackendFromValue filters one etcd model mapping. Returns
// (backend was removed, model was deleted, value was valid JSON).
func removeBackendFromValue(ctx context.Context, client *clientv3.Client, key, name string, raw []byte, target string) (updated, deleted, valid bool) {
	var v etcdModelValue
	if err := json.Unmarshal(raw, &v); err != nil {
		fmt.Fprintf(os.Stderr, "warning: %s: invalid JSON, skipped: %v\n", name, err)
		return false, false, false
	}

	filtered, removed := filterBackends(v.Backends, target)
	if !removed {
		return false, false, true
	}
	if len(filtered) == 0 {
		deleteEtcdKey(ctx, client, key)
		fmt.Printf("  deleted model %s (last backend removed)\n", name)
		return true, true, true
	}
	v.Backends = filtered
	mustPutEtcdModelValue(client, key, v)
	fmt.Printf("  removed backend from %s\n", name)
	return true, false, true
}
