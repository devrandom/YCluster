package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// runCLI dispatches the "models" / "backends" subcommands. The default
// config path is used unless -config was given before the subcommand,
// so invocations look like:
//
//	local-ai-proxy models ls
//	local-ai-proxy backends disable http://x1.xc:8080
func runCLI(subcmd string, args []string, configPath string) {
	cfg := mustLoadConfig(configPath)
	if cfg.Etcd == nil || cfg.Etcd.Prefix == "" {
		fatal("CLI requires etcd-backed config (no etcd section in %s)", configPath)
	}
	client := mustEtcdClient(cfg.Etcd)
	defer client.Close()

	switch subcmd {
	case "models":
		runModels(args, client, cfg.Etcd.Prefix)
	case "backends":
		prefix := cfg.Etcd.DisabledPrefix
		if prefix == "" {
			prefix = DefaultDisabledPrefix
		}
		runBackends(args, client, prefix)
	default:
		fatal("unknown subcommand %q", subcmd)
	}
}

func runModels(args []string, client *clientv3.Client, prefix string) {
	if len(args) == 0 {
		fatalUsage("models <ls|add|remove>")
	}
	switch args[0] {
	case "ls", "list":
		modelsLs(client, prefix)
	case "add":
		modelsAdd(args[1:], client, prefix)
	case "remove", "rm":
		modelsRemove(args[1:], client, prefix)
	default:
		fatalUsage("models <ls|add|remove>")
	}
}

func runBackends(args []string, client *clientv3.Client, prefix string) {
	if len(args) == 0 {
		fatalUsage("backends <disable|enable|ls>")
	}
	switch args[0] {
	case "disable":
		backendsDisable(args[1:], client, prefix)
	case "enable":
		backendsEnable(args[1:], client, prefix)
	case "ls", "list":
		backendsLs(client, prefix)
	default:
		fatalUsage("backends <disable|enable|ls>")
	}
}

func modelsLs(client *clientv3.Client, prefix string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Get(ctx, prefix, clientv3.WithPrefix())
	if err != nil {
		fatal("etcd get %s: %v", prefix, err)
	}
	if len(resp.Kvs) == 0 {
		fmt.Println("No models configured.")
		return
	}
	type entry struct {
		name     string
		backends []string
	}
	entries := make([]entry, 0, len(resp.Kvs))
	for _, kv := range resp.Kvs {
		name := strings.TrimPrefix(string(kv.Key), prefix)
		var v etcdModelValue
		if err := json.Unmarshal(kv.Value, &v); err != nil {
			fmt.Fprintf(os.Stderr, "warning: %s: invalid JSON: %v\n", name, err)
			continue
		}
		urls := make([]string, 0, len(v.Backends))
		for _, b := range v.Backends {
			urls = append(urls, b.APIBase)
		}
		entries = append(entries, entry{name: name, backends: urls})
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].name < entries[j].name })
	for _, e := range entries {
		if len(e.backends) == 1 {
			fmt.Printf("  %s  ->  %s\n", e.name, e.backends[0])
		} else {
			fmt.Printf("  %s  (%d backends)\n", e.name, len(e.backends))
			for _, b := range e.backends {
				fmt.Printf("    - %s\n", b)
			}
		}
	}
}

func modelsAdd(args []string, client *clientv3.Client, prefix string) {
	fs := flag.NewFlagSet("models add", flag.ExitOnError)
	fs.Usage = func() { fmt.Fprintln(os.Stderr, "usage: local-ai-proxy models add <api-base> [model]") }
	_ = fs.Parse(args)
	rest := fs.Args()
	if len(rest) == 0 || len(rest) > 2 {
		fs.Usage()
		os.Exit(2)
	}
	apiBase, err := normalizeBackendURL(rest[0])
	if err != nil {
		fatal("%v", err)
	}
	if len(rest) == 2 {
		modelsAddOne(client, prefix, rest[1], apiBase)
		return
	}
	// Auto-discover.
	ids, err := discoverModels(apiBase)
	if err != nil {
		fatal("%v", err)
	}
	if len(ids) == 0 {
		fatal("backend at %s returned no models", apiBase)
	}
	added, skipped := 0, 0
	for _, id := range ids {
		ok := modelsAddOneResult(client, prefix, id, apiBase)
		if ok {
			fmt.Printf("  added: %s -> %s\n", id, apiBase)
			added++
		} else {
			fmt.Printf("  skip:  %s (already configured for %s)\n", id, apiBase)
			skipped++
		}
	}
	fmt.Printf("\nAdded %d, skipped %d.\n", added, skipped)
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
	resp, err := client.Get(ctx, key)
	if err != nil {
		fatal("etcd get %s: %v", key, err)
	}
	var v etcdModelValue
	if len(resp.Kvs) > 0 {
		if err := json.Unmarshal(resp.Kvs[0].Value, &v); err != nil {
			fatal("existing value at %s is not JSON: %v", key, err)
		}
		for _, b := range v.Backends {
			if b.APIBase == apiBase {
				return false
			}
		}
	}
	v.Backends = append(v.Backends, etcdBackendEntry{APIBase: apiBase})
	buf, err := json.Marshal(v)
	if err != nil {
		fatal("marshal: %v", err)
	}
	if _, err := client.Put(ctx, key, string(buf)); err != nil {
		fatal("etcd put %s: %v", key, err)
	}
	return true
}

func modelsRemove(args []string, client *clientv3.Client, prefix string) {
	fs := flag.NewFlagSet("models remove", flag.ExitOnError)
	apiBaseFlag := fs.String("api-base", "", "remove only this backend instead of the whole model")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: local-ai-proxy models remove <model> [--api-base <url>]")
	}
	_ = fs.Parse(args)
	rest := fs.Args()
	if len(rest) != 1 {
		fs.Usage()
		os.Exit(2)
	}
	model := rest[0]
	key := prefix + model

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Get(ctx, key)
	if err != nil {
		fatal("etcd get %s: %v", key, err)
	}
	if len(resp.Kvs) == 0 {
		fatal("no such model: %s", model)
	}

	if *apiBaseFlag == "" {
		if _, err := client.Delete(ctx, key); err != nil {
			fatal("etcd delete %s: %v", key, err)
		}
		fmt.Printf("Removed model: %s\n", model)
		return
	}

	target, err := normalizeBackendURL(*apiBaseFlag)
	if err != nil {
		fatal("%v", err)
	}
	var v etcdModelValue
	if err := json.Unmarshal(resp.Kvs[0].Value, &v); err != nil {
		fatal("existing value at %s is not JSON: %v", key, err)
	}
	filtered := v.Backends[:0]
	removed := false
	for _, b := range v.Backends {
		if b.APIBase == target {
			removed = true
			continue
		}
		filtered = append(filtered, b)
	}
	if !removed {
		fatal("model %s has no backend %s", model, target)
	}
	if len(filtered) == 0 {
		if _, err := client.Delete(ctx, key); err != nil {
			fatal("etcd delete %s: %v", key, err)
		}
		fmt.Printf("Removed last backend from %s — model deleted.\n", model)
		return
	}
	v.Backends = filtered
	buf, err := json.Marshal(v)
	if err != nil {
		fatal("marshal: %v", err)
	}
	if _, err := client.Put(ctx, key, string(buf)); err != nil {
		fatal("etcd put %s: %v", key, err)
	}
	fmt.Printf("Removed backend %s from %s.\n", target, model)
}

func backendsDisable(args []string, client *clientv3.Client, prefix string) {
	fs := flag.NewFlagSet("backends disable", flag.ExitOnError)
	reason := fs.String("reason", "", "human-readable reason (stored as JSON metadata)")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: local-ai-proxy backends disable <url> [--reason ...]")
	}
	_ = fs.Parse(args)
	rest := fs.Args()
	if len(rest) != 1 {
		fs.Usage()
		os.Exit(2)
	}
	target, err := normalizeBackendURL(rest[0])
	if err != nil {
		fatal("%v", err)
	}
	key := prefix + target
	value := ""
	if *reason != "" {
		buf, err := json.Marshal(map[string]string{
			"reason": *reason,
			"at":     time.Now().UTC().Format(time.RFC3339),
		})
		if err != nil {
			fatal("marshal: %v", err)
		}
		value = string(buf)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := client.Put(ctx, key, value); err != nil {
		fatal("etcd put %s: %v", key, err)
	}
	fmt.Printf("Disabled: %s\n", target)
	if *reason != "" {
		fmt.Printf("  reason: %s\n", *reason)
	}
	fmt.Println("(takes effect at next health-check cycle)")
}

func backendsEnable(args []string, client *clientv3.Client, prefix string) {
	if len(args) != 1 {
		fatalUsage("backends enable <url>")
	}
	target, err := normalizeBackendURL(args[0])
	if err != nil {
		fatal("%v", err)
	}
	key := prefix + target
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Delete(ctx, key)
	if err != nil {
		fatal("etcd delete %s: %v", key, err)
	}
	if resp.Deleted == 0 {
		fmt.Printf("Was not disabled: %s\n", target)
		return
	}
	fmt.Printf("Enabled: %s\n", target)
	fmt.Println("(takes effect at next health-check cycle)")
}

func backendsLs(client *clientv3.Client, prefix string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Get(ctx, prefix, clientv3.WithPrefix())
	if err != nil {
		fatal("etcd get %s: %v", prefix, err)
	}
	if len(resp.Kvs) == 0 {
		fmt.Println("No backends disabled.")
		return
	}
	for _, kv := range resp.Kvs {
		u := strings.TrimPrefix(string(kv.Key), prefix)
		meta := strings.TrimSpace(string(kv.Value))
		if meta == "" {
			fmt.Printf("  %s\n", u)
		} else {
			fmt.Printf("  %s  %s\n", u, meta)
		}
	}
}

// normalizeBackendURL canonicalises a user-provided URL to the form
// stored in etcd: "scheme://host[:port]" with no path, no trailing
// slash. Scheme defaults to http; we don't attempt to guess a port.
func normalizeBackendURL(s string) (string, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return "", errors.New("empty URL")
	}
	if !strings.Contains(s, "://") {
		s = "http://" + s
	}
	u, err := url.Parse(s)
	if err != nil {
		return "", fmt.Errorf("invalid URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("invalid URL %q: missing host", s)
	}
	out := u.Scheme + "://" + u.Host
	return out, nil
}

// discoverModels queries <api-base>/v1/models and returns the list of
// model IDs. Used by `models add <api-base>` with no explicit model.
func discoverModels(apiBase string) ([]string, error) {
	endpoint := apiBase + "/v1/models"
	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", endpoint, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("%s returned HTTP %d: %s", endpoint, resp.StatusCode, string(body))
	}
	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode %s: %w", endpoint, err)
	}
	ids := make([]string, 0, len(payload.Data))
	for _, m := range payload.Data {
		if m.ID != "" {
			ids = append(ids, m.ID)
		}
	}
	return ids, nil
}

func mustLoadConfig(path string) Config {
	cfg, err := LoadConfig(path)
	if err != nil {
		fatal("%v", err)
	}
	return cfg
}

func mustEtcdClient(cfg *EtcdConfig) *clientv3.Client {
	endpoints := cfg.Endpoints
	if len(endpoints) == 0 {
		endpoints = []string{DefaultEtcdEndpoint}
	}
	client, err := clientv3.New(clientv3.Config{
		Endpoints:   endpoints,
		DialTimeout: 5 * time.Second,
		Username:    cfg.Username,
		Password:    cfg.Password,
	})
	if err != nil {
		fatal("etcd client: %v", err)
	}
	return client
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func fatalUsage(usage string) {
	fmt.Fprintln(os.Stderr, "usage: local-ai-proxy "+usage)
	os.Exit(2)
}
