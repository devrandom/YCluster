package main

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync/atomic"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// EtcdDisabledBackends tracks the set of operator-disabled backend URLs
// from an etcd prefix. Refresh is called explicitly (once per health-
// check cycle) rather than via watch — disable changes are infrequent
// and reactivity at 30s is acceptable.
//
// Keys under the prefix are the raw backend URL (same convention as the
// model prefix); values hold optional metadata JSON that is ignored by
// the proxy but useful for humans (reason, date).
type EtcdDisabledBackends struct {
	client *clientv3.Client
	prefix string
	logger *slog.Logger
	set    atomic.Pointer[map[string]struct{}]
}

func NewEtcdDisabledBackends(client *clientv3.Client, prefix string, logger *slog.Logger) *EtcdDisabledBackends {
	return &EtcdDisabledBackends{client: client, prefix: prefix, logger: logger}
}

func (d *EtcdDisabledBackends) Refresh(ctx context.Context) error {
	resp, err := d.client.Get(ctx, d.prefix, clientv3.WithPrefix(), clientv3.WithKeysOnly())
	if err != nil {
		return fmt.Errorf("etcd get %s: %w", d.prefix, err)
	}
	next := make(map[string]struct{}, len(resp.Kvs))
	for _, kv := range resp.Kvs {
		url := strings.TrimPrefix(string(kv.Key), d.prefix)
		if url == "" {
			continue
		}
		next[url] = struct{}{}
	}
	d.set.Store(&next)
	return nil
}

func (d *EtcdDisabledBackends) IsDisabled(url string) bool {
	m := d.set.Load()
	if m == nil {
		return false
	}
	_, ok := (*m)[url]
	return ok
}

// DefaultDisabledPrefix is the sibling of the models prefix under the
// ycluster inference tree.
const DefaultDisabledPrefix = "/cluster/config/inference/disabled/"
