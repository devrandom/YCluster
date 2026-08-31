package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
)

type modelEntry struct {
	name     string
	backends []string
}

func modelsLs(configPath string, args []string) {
	if len(args) != 0 {
		fatalUsage("models ls")
	}
	cfg := mustLoadConfig(configPath)
	if cfg.Etcd == nil || cfg.Etcd.Prefix == "" {
		fatal("CLI requires etcd-backed config (no etcd section in %s)", configPath)
	}
	client := mustEtcdClient(cfg.Etcd)
	defer client.Close()
	prefix := cfg.Etcd.Prefix

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
	entries := parseModelEntries(resp.Kvs, prefix)
	sort.Slice(entries, func(i, j int) bool { return entries[i].name < entries[j].name })
	printModelEntries(entries)
}

// parseModelEntries converts etcd key-value pairs into sorted model entries,
// skipping invalid JSON with a stderr warning.
func parseModelEntries(kvs []*mvccpb.KeyValue, prefix string) []modelEntry {
	entries := make([]modelEntry, 0, len(kvs))
	for _, kv := range kvs {
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
		entries = append(entries, modelEntry{name: name, backends: urls})
	}
	return entries
}

func printModelEntries(entries []modelEntry) {
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

type modelACLEntry struct {
	model   string
	entries []ACLEntry
}

func aclLs(args []string, configPath string) {
	if len(args) != 0 {
		fatalUsage("acl ls")
	}
	cfg := mustLoadConfig(configPath)
	if cfg.Etcd == nil {
		fatal("acl ls requires etcd config")
	}
	client := mustEtcdClient(cfg.Etcd)
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.Get(ctx, cfg.Etcd.Prefix, clientv3.WithPrefix())
	if err != nil {
		fatal("etcd get %s: %v", cfg.Etcd.Prefix, err)
	}
	ac := parseACLEntriesFromKVs(resp.Kvs, cfg.Etcd.Prefix)
	sort.Slice(ac, func(i, j int) bool { return ac[i].model < ac[j].model })
	printACLEntries(ac)
}

// parseACLEntriesFromKVs extracts per-model ACL rules from etcd key-values,
// skipping invalid JSON and models without ACL rules.
func parseACLEntriesFromKVs(kvs []*mvccpb.KeyValue, prefix string) []modelACLEntry {
	var ac []modelACLEntry
	for _, kv := range kvs {
		name := strings.TrimPrefix(string(kv.Key), prefix)
		var v etcdModelValue
		if err := json.Unmarshal(kv.Value, &v); err != nil {
			continue
		}
		if v.ACL != nil {
			ac = append(ac, modelACLEntry{model: name, entries: v.ACL.Entries})
		}
	}
	return ac
}

func printACLEntries(ac []modelACLEntry) {
	if len(ac) == 0 {
		fmt.Println("No per-model ACL rules.")
		return
	}
	for i, e := range ac {
		if i > 0 {
			fmt.Println()
		}
		fmt.Printf("%s:\n", e.model)
		for _, entry := range e.entries {
			symbol := "+"
			if entry.Decision == ACLDeny {
				symbol = "-"
			}
			fmt.Printf("  %s%s\n", symbol, entry.Subject)
		}
	}
}
