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
	switch subcmd {
	case "models":
		runModels(args, configPath)
	case "backends":
		runBackends(args, configPath)
	case "acl":
		runACL(args, configPath)
	default:
		fatal("unknown subcommand %q", subcmd)
	}
}

func runModels(args []string, configPath string) {
	if len(args) == 0 {
		fatalUsage("models <ls|add|remove>")
	}
	subcmd := args[0]
	args = args[1:]

	switch subcmd {
	case "ls", "list":
		modelsLs(configPath, args)
	case "add":
		modelsAdd(args, configPath)
	case "remove", "rm":
		modelsRemove(args, configPath)
	default:
		fatalUsage("models <ls|add|remove>")
	}
}

func runBackends(args []string, configPath string) {
	if len(args) == 0 {
		fatalUsage("backends <disable|enable|ls>")
	}
	subcmd := args[0]
	args = args[1:]

	switch subcmd {
	case "disable":
		backendsDisable(args, configPath)
	case "enable":
		backendsEnable(args, configPath)
	case "ls", "list":
		backendsLs(args, configPath)
	default:
		fatalUsage("backends <disable|enable|ls>")
	}
}

// validateModelName rejects model names that are almost certainly an argument
// mistake: empty, or URL-shaped (an api-base passed where the model name
// belongs). `models add` takes <api-base> [model]; swapping the two otherwise
// writes a junk URL-keyed model entry, which then trips `models ls`.
func validateModelName(model string) error {
	if strings.TrimSpace(model) == "" {
		return errors.New("empty model name")
	}
	if strings.Contains(model, "://") {
		return fmt.Errorf("model name %q looks like a URL; usage is `models add <api-base> [model]` (api-base first)", model)
	}
	return nil
}

func modelsAdd(args []string, configPath string) {
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
	client, prefix := mustEtcdForCLI(configPath)
	defer client.Close()
	if len(rest) == 2 {
		if err := validateModelName(rest[1]); err != nil {
			fatal("%v", err)
		}
		modelsAddOne(client, prefix, rest[1], apiBase)
		return
	}
	modelsAddDiscovered(client, prefix, apiBase)
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
	added, skipped := 0, 0
	for _, id := range ids {
		if err := validateModelName(id); err != nil {
			fmt.Printf("  skip:  %s (%v)\n", id, err)
			skipped++
			continue
		}
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

	touched, deleted := 0, 0
	for _, kv := range resp.Kvs {
		name := strings.TrimPrefix(string(kv.Key), prefix)
		updated, modelDeleted, ok := removeBackendFromValue(ctx, client, string(kv.Key), name, kv.Value, target)
		if !ok {
			continue
		}
		if updated {
			touched++
		}
		if modelDeleted {
			deleted++
		}
	}
	if touched == 0 {
		fmt.Printf("No models reference backend %s\n", target)
		return
	}
	fmt.Printf("Removed backend %s from %d model(s); %d model(s) deleted.\n", target, touched, deleted)
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

func backendsDisable(args []string, configPath string) {
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
	client, prefix := mustEtcdForCLIDisabled(configPath)
	defer client.Close()
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

func backendsEnable(args []string, configPath string) {
	if len(args) != 1 {
		fatalUsage("backends enable <url>")
	}
	target, err := normalizeBackendURL(args[0])
	if err != nil {
		fatal("%v", err)
	}
	client, prefix := mustEtcdForCLIDisabled(configPath)
	defer client.Close()
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

func backendsLs(args []string, configPath string) {
	if len(args) != 0 {
		fatalUsage("backends ls")
	}
	client, prefix := mustEtcdForCLIDisabled(configPath)
	defer client.Close()
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
	// A backend URL is scheme://host[:port] only. A path/query/fragment almost
	// always means a model name ("org/model") was passed where a backend URL
	// was expected; reject it rather than silently truncating to the host,
	// which lands a junk disabled/model entry that matches no real backend.
	if strings.Trim(u.Path, "/") != "" || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("invalid backend URL %q: expected scheme://host[:port] with no path (did you mean a model name?)", s)
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

func mustEtcdForCLI(path string) (*clientv3.Client, string) {
	cfg := mustLoadConfig(path)
	if cfg.Etcd == nil || cfg.Etcd.Prefix == "" {
		fatal("CLI requires etcd-backed config (no etcd section in %s)", path)
	}
	client := mustEtcdClient(cfg.Etcd)
	return client, cfg.Etcd.Prefix
}

// disabledPrefixFor resolves the etcd prefix for operator-disabled backends,
// matching the gateway's resolution in main.go (EtcdConfig.DisabledPrefix, or
// DefaultDisabledPrefix when unset). The CLI MUST agree with the gateway, or a
// `backends disable` lands where the gateway never looks — and pollutes the
// models prefix instead.
func disabledPrefixFor(cfg *EtcdConfig) string {
	if cfg.DisabledPrefix != "" {
		return cfg.DisabledPrefix
	}
	return DefaultDisabledPrefix
}

// mustEtcdForCLIDisabled is mustEtcdForCLI for the `backends` subcommands: it
// returns the disabled-backends prefix instead of the models prefix.
func mustEtcdForCLIDisabled(path string) (*clientv3.Client, string) {
	cfg := mustLoadConfig(path)
	if cfg.Etcd == nil || cfg.Etcd.Prefix == "" {
		fatal("CLI requires etcd-backed config (no etcd section in %s)", path)
	}
	client := mustEtcdClient(cfg.Etcd)
	return client, disabledPrefixFor(cfg.Etcd)
}

func mustEtcdClient(cfg *EtcdConfig) *clientv3.Client {
	client, err := newEtcdClient(cfg)
	if err != nil {
		fatal("etcd client: %v", err)
	}
	return client
}

func runACL(args []string, configPath string) {
	if len(args) == 0 {
		fatalUsage("acl <ls|set>")
	}
	subcmd := args[0]
	args = args[1:]

	switch subcmd {
	case "ls":
		aclLs(args, configPath)
	case "set":
		aclSet(args, configPath)
	default:
		fatalUsage("acl <ls|set>")
	}
}

func aclSet(args []string, configPath string) {
	model := ""
	tokens := []string{}
	if len(args) >= 1 {
		model = args[0]
		tokens = args[1:]
	}
	if model == "" {
		fatalUsage("acl set <model> [+-user:name|+-group:name...]")
	}

	entries, err := ParseACLEntries(tokens)
	if err != nil {
		fatal("%v", err)
	}

	client, prefix := mustEtcdForCLI(configPath)
	defer client.Close()
	key := prefix + model
	value := mustGetEtcdModelValue(client, key, model)

	if len(tokens) == 0 {
		value.ACL = nil
	} else {
		value.ACL = &ACLModelRule{Entries: entries}
	}
	mustPutEtcdModelValue(client, key, value)
	printACLSet(model, value.ACL)
}

// mustGetEtcdModelValue reads one model mapping and turns a missing key or
// malformed JSON into the CLI's usual fatal error.
func mustGetEtcdModelValue(client *clientv3.Client, key, model string) etcdModelValue {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Get(ctx, key)
	if err != nil {
		fatal("etcd get %s: %v", key, err)
	}
	if len(resp.Kvs) == 0 {
		fatal("no such model: %s", model)
	}

	var value etcdModelValue
	if err := json.Unmarshal(resp.Kvs[0].Value, &value); err != nil {
		fatal("existing value at %s is not JSON: %v", key, err)
	}
	return value
}

func mustPutEtcdModelValue(client *clientv3.Client, key string, value etcdModelValue) {
	buf, err := json.Marshal(value)
	if err != nil {
		fatal("marshal: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := client.Put(ctx, key, string(buf)); err != nil {
		fatal("etcd put %s: %v", key, err)
	}
}

func printACLSet(model string, rule *ACLModelRule) {
	if rule == nil {
		fmt.Printf("ACL cleared for %s\n", model)
		return
	}
	fmt.Printf("ACL updated for %s:\n", model)
	for _, entry := range rule.Entries {
		prefix := "+"
		if entry.Decision == ACLDeny {
			prefix = "-"
		}
		fmt.Printf("  %s%s\n", prefix, entry.Subject)
	}
}

func fatalUsage(usage string) {
	fmt.Fprintln(os.Stderr, "usage: local-ai-proxy "+usage)
	os.Exit(2)
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
