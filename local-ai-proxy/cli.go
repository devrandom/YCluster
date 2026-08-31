package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
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
