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

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
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

func backendsDisable(args []string, configPath string) {
	fs := flag.NewFlagSet("backends disable", flag.ExitOnError)
	reason := fs.String("reason", "", "human-readable reason (stored as JSON metadata)")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: local-ai-proxy backends disable <url> [--reason ...]")
	}
	_ = fs.Parse(args)
	target, err := normalizeBackendURL(disableTarget(fs.Args(), fs))
	if err != nil {
		fatal("%v", err)
	}

	client, prefix := mustEtcdForCLIDisabled(configPath)
	defer client.Close()
	putDisabledBackend(client, prefix, target, disableMetadataJSON(*reason))
	printDisabled(*reason, target)
}

// disableTarget validates exactly one positional backend URL.
func disableTarget(rest []string, fs *flag.FlagSet) string {
	if len(rest) != 1 {
		fs.Usage()
		os.Exit(2)
	}
	return rest[0]
}

// putDisabledBackend writes the disabled marker for a backend URL.
func putDisabledBackend(client *clientv3.Client, prefix, target, value string) {
	key := prefix + target
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := client.Put(ctx, key, value); err != nil {
		fatal("etcd put %s: %v", key, err)
	}
}

// printDisabled reports the successful disable operation.
func printDisabled(reason, target string) {
	fmt.Printf("Disabled: %s\n", target)
	if reason != "" {
		fmt.Printf("  reason: %s\n", reason)
	}
	fmt.Println("(takes effect at next health-check cycle)")
}

// disableMetadataJSON marshals the optional disable reason to JSON metadata,
// or an empty string when no reason was given.
func disableMetadataJSON(reason string) string {
	if reason == "" {
		return ""
	}
	buf, err := json.Marshal(map[string]string{
		"reason": reason,
		"at":     time.Now().UTC().Format(time.RFC3339),
	})
	if err != nil {
		fatal("marshal: %v", err)
	}
	return string(buf)
}

func backendsEnable(args []string, configPath string) {
	enableTarget(args)
	target, err := normalizeBackendURL(args[0])
	if err != nil {
		fatal("%v", err)
	}
	client, prefix := mustEtcdForCLIDisabled(configPath)
	defer client.Close()
	deleteDisabledBackend(client, prefix, target)
}

// enableTarget validates the one positional backend URL.
func enableTarget(args []string) {
	if len(args) != 1 {
		fatalUsage("backends enable <url>")
	}
}

// deleteDisabledBackend removes the disable marker and reports the result.
func deleteDisabledBackend(client *clientv3.Client, prefix, target string) {
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
		printDisabledBackend(prefix, kv)
	}
}

// printDisabledBackend renders one disabled-backend key with its optional note.
func printDisabledBackend(prefix string, kv *mvccpb.KeyValue) {
	u := strings.TrimPrefix(string(kv.Key), prefix)
	meta := strings.TrimSpace(string(kv.Value))
	if meta == "" {
		fmt.Printf("  %s\n", u)
		return
	}
	fmt.Printf("  %s  %s\n", u, meta)
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
	return decodeModelIDs(resp, endpoint)
}

// decodeModelIDs validates the response status and extracts model IDs.
func decodeModelIDs(resp *http.Response, endpoint string) ([]string, error) {
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("%s returned HTTP %d: %s", endpoint, resp.StatusCode, string(body))
	}
	payload, err := decodeModelPayload(resp, endpoint)
	if err != nil {
		return nil, err
	}
	return modelIDs(payload), nil
}

// decodeModelPayload reads the standard OpenAI-style models response.
func decodeModelPayload(resp *http.Response, endpoint string) ([]struct {
	ID string `json:"id"`
}, error) {
	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode %s: %w", endpoint, err)
	}
	return payload.Data, nil
}

func modelIDs(data []struct {
	ID string `json:"id"`
}) []string {
	ids := make([]string, 0, len(data))
	for _, m := range data {
		if m.ID != "" {
			ids = append(ids, m.ID)
		}
	}
	return ids
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
	model, tokens := aclSetArgs(args)
	entries, err := ParseACLEntries(tokens)
	if err != nil {
		fatal("%v", err)
	}

	client, prefix := mustEtcdForCLI(configPath)
	defer client.Close()
	key := prefix + model
	value := mustGetEtcdModelValue(client, key, model)
	value.ACL = aclRule(entries, len(tokens))
	mustPutEtcdModelValue(client, key, value)
	printACLSet(model, value.ACL)
}

// aclSetArgs splits the model name from ACL entry tokens.
func aclSetArgs(args []string) (model string, tokens []string) {
	if len(args) >= 1 {
		model = args[0]
		tokens = args[1:]
	}
	if model == "" {
		fatalUsage("acl set <model> [+-user:name|+-group:name...]")
	}
	return model, tokens
}

// aclRule returns nil to clear the ACL, or a rule containing the entries.
func aclRule(entries []ACLEntry, count int) *ACLModelRule {
	if count == 0 {
		return nil
	}
	return &ACLModelRule{Entries: entries}
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
