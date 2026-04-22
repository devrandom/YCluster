package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/url"
	"strings"
	"sync/atomic"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// Source produces a map of model name → ordered list of backend URLs.
// Today the router uses only the first URL; future work will pick among
// them. The returned map from Snapshot must not be mutated by callers.
type Source interface {
	Start(ctx context.Context) error
	Snapshot() map[string][]*url.URL
	Close() error
}

// YAMLSource is a static mapping loaded from YAML config at startup.
// Duplicate `model:` entries are allowed — their api_bases accumulate
// in insertion order under the same model name.
type YAMLSource struct {
	m map[string][]*url.URL
}

func NewYAMLSource(mappings []Mapping) (*YAMLSource, error) {
	m := make(map[string][]*url.URL, len(mappings))
	for _, mp := range mappings {
		if mp.Model == "" || mp.APIBase == "" {
			return nil, fmt.Errorf("backends entry must have non-empty model and api_base")
		}
		u, err := url.Parse(mp.APIBase)
		if err != nil {
			return nil, fmt.Errorf("backends[%q].api_base: %w", mp.Model, err)
		}
		m[mp.Model] = append(m[mp.Model], u)
	}
	return &YAMLSource{m: m}, nil
}

func (s *YAMLSource) Start(ctx context.Context) error   { return nil }
func (s *YAMLSource) Snapshot() map[string][]*url.URL   { return s.m }
func (s *YAMLSource) Close() error                      { return nil }

// etcdBackendEntry is one backend within an etcd model value.
// Future fields (backend_model, max_concurrent, weight) will live here.
type etcdBackendEntry struct {
	APIBase string `json:"api_base"`
}

// etcdModelValue is the JSON value stored at each etcd key. The schema
// is future-proof for fan-out: router currently uses backends[0] only.
type etcdModelValue struct {
	Backends []etcdBackendEntry `json:"backends"`
}

// EtcdSource watches an etcd prefix. Each key under the prefix represents
// one model; the suffix after the prefix is the model name. The value is
// JSON matching etcdValue.
type EtcdSource struct {
	client *clientv3.Client
	prefix string
	logger *slog.Logger

	// snapshot holds an immutable map; swapped atomically on updates.
	snapshot atomic.Pointer[map[string][]*url.URL]

	cancel context.CancelFunc
	done   chan struct{}
}

// DefaultEtcdEndpoint is used when etcd.endpoints is empty. Matches the
// ycluster convention of a core-local etcd on each storage node.
const DefaultEtcdEndpoint = "http://localhost:2379"

func NewEtcdSource(cfg EtcdConfig, logger *slog.Logger) (*EtcdSource, error) {
	if cfg.Prefix == "" {
		return nil, fmt.Errorf("etcd.prefix is required")
	}
	if len(cfg.Endpoints) == 0 {
		cfg.Endpoints = []string{DefaultEtcdEndpoint}
	}
	client, err := clientv3.New(clientv3.Config{
		Endpoints:   cfg.Endpoints,
		DialTimeout: 5 * time.Second,
		Username:    cfg.Username,
		Password:    cfg.Password,
	})
	if err != nil {
		return nil, fmt.Errorf("etcd client: %w", err)
	}
	return &EtcdSource{
		client: client,
		prefix: cfg.Prefix,
		logger: logger,
	}, nil
}

func (s *EtcdSource) Start(ctx context.Context) error {
	getResp, err := s.client.Get(ctx, s.prefix, clientv3.WithPrefix())
	if err != nil {
		return fmt.Errorf("initial etcd get %s: %w", s.prefix, err)
	}
	m := make(map[string][]*url.URL, len(getResp.Kvs))
	for _, kv := range getResp.Kvs {
		name := s.modelName(kv.Key)
		if urls, ok := s.parseKV(kv.Key, kv.Value); ok {
			m[name] = urls
			if len(urls) > 1 {
				s.logger.Warn("model has multiple backends; using first until fan-out is implemented",
					"model", name, "count", len(urls))
			}
		}
	}
	s.snapshot.Store(&m)
	s.logger.Info("etcd source initial load", "prefix", s.prefix, "models", len(m))

	watchCtx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.done = make(chan struct{})
	startRev := getResp.Header.Revision + 1
	go s.watch(watchCtx, startRev)
	return nil
}

func (s *EtcdSource) watch(ctx context.Context, startRev int64) {
	defer close(s.done)
	ch := s.client.Watch(ctx, s.prefix, clientv3.WithPrefix(), clientv3.WithRev(startRev))
	for wresp := range ch {
		if err := wresp.Err(); err != nil {
			s.logger.Warn("etcd watch error", "err", err.Error())
			continue
		}
		if len(wresp.Events) == 0 {
			continue
		}
		cur := s.snapshot.Load()
		next := make(map[string][]*url.URL, len(*cur)+len(wresp.Events))
		for k, v := range *cur {
			next[k] = v
		}
		for _, ev := range wresp.Events {
			name := s.modelName(ev.Kv.Key)
			switch ev.Type {
			case clientv3.EventTypePut:
				if urls, ok := s.parseKV(ev.Kv.Key, ev.Kv.Value); ok {
					next[name] = urls
					if len(urls) > 1 {
						s.logger.Warn("model updated with multiple backends; using first until fan-out is implemented",
							"model", name, "count", len(urls))
					}
				}
			case clientv3.EventTypeDelete:
				delete(next, name)
			}
		}
		s.snapshot.Store(&next)
		s.logger.Info("etcd source updated", "models", len(next))
	}
}

func (s *EtcdSource) parseKV(key, value []byte) ([]*url.URL, bool) {
	var v etcdModelValue
	if err := json.Unmarshal(value, &v); err != nil {
		s.logger.Warn("etcd value not JSON", "key", string(key), "err", err.Error())
		return nil, false
	}
	if len(v.Backends) == 0 {
		s.logger.Warn("etcd value has empty backends list", "key", string(key))
		return nil, false
	}
	urls := make([]*url.URL, 0, len(v.Backends))
	for i, b := range v.Backends {
		if b.APIBase == "" {
			s.logger.Warn("etcd backend entry missing api_base", "key", string(key), "index", i)
			continue
		}
		u, err := url.Parse(b.APIBase)
		if err != nil {
			s.logger.Warn("etcd api_base is not a URL", "key", string(key), "index", i, "err", err.Error())
			continue
		}
		urls = append(urls, u)
	}
	if len(urls) == 0 {
		return nil, false
	}
	return urls, true
}

func (s *EtcdSource) modelName(key []byte) string {
	return strings.TrimPrefix(string(key), s.prefix)
}

func (s *EtcdSource) Snapshot() map[string][]*url.URL {
	if m := s.snapshot.Load(); m != nil {
		return *m
	}
	return nil
}

func (s *EtcdSource) Close() error {
	if s.cancel != nil {
		s.cancel()
	}
	if s.done != nil {
		<-s.done
	}
	return s.client.Close()
}

// Client exposes the underlying etcd client so adjacent components
// (e.g., EtcdDisabledBackends) can share it without reopening.
func (s *EtcdSource) Client() *clientv3.Client { return s.client }
