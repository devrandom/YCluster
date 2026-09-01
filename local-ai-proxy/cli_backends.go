package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
)

type backendCommand func(args []string, configPath string)

var backendCommands = map[string]backendCommand{
	"disable": backendsDisable,
	"enable":  backendsEnable,
	"ls":      backendsLs,
	"list":    backendsLs,
}

func runBackends(args []string, configPath string) {
	subcmd, rest := popSubcommand(args, "backends <disable|enable|ls>")
	cmd, ok := backendCommands[subcmd]
	if !ok {
		fatalUsage("backends <disable|enable|ls>")
	}
	cmd(rest, configPath)
}

// popSubcommand splits and validates the command word after a top-level
// subcommand. It exits with usage when the word is missing.
func popSubcommand(args []string, usage string) (string, []string) {
	if len(args) == 0 {
		fatalUsage(usage)
	}
	return args[0], args[1:]
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
	kvs := disabledBackendKVs(client, prefix)
	if len(kvs) == 0 {
		fmt.Println("No backends disabled.")
		return
	}
	for _, kv := range kvs {
		printDisabledBackend(prefix, kv)
	}
}

// disabledBackendKVs returns all disabled-backend key-values.
func disabledBackendKVs(client *clientv3.Client, prefix string) []*mvccpb.KeyValue {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Get(ctx, prefix, clientv3.WithPrefix())
	if err != nil {
		fatal("etcd get %s: %v", prefix, err)
	}
	return resp.Kvs
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
