package main

import (
	"errors"
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

// DefaultHealthCheckInterval is used when health_check_interval is unset.
const DefaultHealthCheckInterval = 30 * time.Second

// DefaultAuthHealthURL is the auth sidecar's /healthz endpoint, on the
// same host as the proxy (both are leader-elected together). The proxy's
// /healthz probes it and reports auth status, returning 503 when the
// auth service is down. The sidecar always listens on 127.0.0.1:4002.
const DefaultAuthHealthURL = "http://127.0.0.1:4002/healthz"

// Config is the top-level YAML schema. Exactly one of backend, backends,
// or etcd must be set.
type Config struct {
	Listen string `yaml:"listen"`

	// MetricsListen is an optional separate listener (addr) that serves
	// only /metrics for Prometheus scraping, e.g. ":9101". When set, /metrics
	// is served from this listener and removed from the main listen address,
	// letting the API listener bind to loopback only while Prometheus still
	// scrapes cross-host. Empty (default) keeps /metrics on the main listener.
	MetricsListen string `yaml:"metrics_listen,omitempty"`

	// Backend: single upstream. All requests forwarded there regardless
	// of model. Useful for a one-vLLM-many-models setup.
	Backend Backend `yaml:"backend"`

	// Backends: static YAML list of model → backend mappings. The proxy
	// reads the "model" field from each request body to pick a backend.
	Backends []Mapping `yaml:"backends"`

	// Etcd: watch an etcd prefix for model → backend mappings. Each key
	// under the prefix is a model name; the JSON value is etcdValue.
	Etcd *EtcdConfig `yaml:"etcd"`

	// HealthCheckInterval is how often the proxy polls each backend's
	// /v1/models endpoint. Zero disables health checks. Default 30s.
	// Only meaningful in model-routed modes (backends or etcd).
	HealthCheckInterval time.Duration `yaml:"health_check_interval"`

	// AuthHealthURL is the auth service's /healthz endpoint. The proxy's
	// own /healthz probes it and reports auth status, returning 503 when
	// the auth service is down. Defaults to the colocated sidecar at
	// DefaultAuthHealthURL; the yaml field is only for non-default
	// deployments.
	AuthHealthURL string `yaml:"auth_health_url,omitempty"`

	// TrustedProxies lists CIDRs whose requests may set X-User-Id
	// (used by downstream logging and, eventually, per-user routing).
	// Requests from any other address get their X-User-Id stripped so
	// clients can't forge identity. Empty/unset defaults to loopback
	// only ("127.0.0.1/32", "::1/128").
	TrustedProxies []string `yaml:"trusted_proxies,omitempty"`

	// ACL is optional per-model access control. When unset (or with
	// default="allow" and no model rules), the proxy is fully open.
	// See acl.go for semantics.
	ACL *ACLConfig `yaml:"acl,omitempty"`

	// MaxRoutingBodyBytes caps the size of a JSON request body the
	// router will buffer to find the `model` field. Defaults to
	// DefaultMaxRoutingBodyBytes (32 MiB). Sized for multimodal
	// chat-completion requests with base64-encoded images; the
	// previous hard-coded 8 MiB cap routinely hit for multi-image
	// prompts. Increase further if you serve heavier payloads.
	// Multipart audio uploads have a separate cap (see router.go).
	MaxRoutingBodyBytes int64 `yaml:"max_routing_body_bytes,omitempty"`
}

// DefaultTrustedProxies applies when trusted_proxies is unset in YAML.
var DefaultTrustedProxies = []string{"127.0.0.1/32", "::1/128"}

type Backend struct {
	URL string `yaml:"url"`
}

type Mapping struct {
	Model   string `yaml:"model"`
	APIBase string `yaml:"api_base"`
}

type EtcdConfig struct {
	Endpoints []string `yaml:"endpoints"`
	Prefix    string   `yaml:"prefix"`
	Username  string   `yaml:"username,omitempty"`
	Password  string   `yaml:"password,omitempty"`
	// DisabledPrefix is the etcd prefix under which each key is the
	// URL of a backend the operator has marked as known-down. Defaults
	// to DefaultDisabledPrefix. Set empty to disable the feature.
	DisabledPrefix string `yaml:"disabled_prefix,omitempty"`
}

func LoadConfig(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read %s: %w", path, err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return Config{}, fmt.Errorf("parse %s: %w", path, err)
	}
	return cfg, nil
}

func LoadConfigOrDefault(configPath, addr, backendURL string) (Config, error) {
	cfg := Config{
		Listen:  ":4000",
		Backend: Backend{URL: "http://localhost:8080"},
	}
	if _, err := os.Stat(configPath); err == nil {
		loaded, err := LoadConfig(configPath)
		if err != nil {
			return Config{}, err
		}
		cfg = loaded
	} else if !errors.Is(err, os.ErrNotExist) || configPath != DefaultConfigPath {
		return Config{}, fmt.Errorf("config %s: %v", configPath, err)
	}
	if addr != "" {
		cfg.Listen = addr
	}
	if backendURL != "" {
		cfg.Backend.URL = backendURL
		cfg.Backends = nil
		cfg.Etcd = nil
	}
	if cfg.MaxRoutingBodyBytes <= 0 {
		cfg.MaxRoutingBodyBytes = DefaultMaxRoutingBodyBytes
	}
	if cfg.AuthHealthURL == "" {
		cfg.AuthHealthURL = DefaultAuthHealthURL
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) Validate() error {
	if c.Listen == "" {
		return fmt.Errorf("listen is required")
	}
	sources := 0
	if c.Backend.URL != "" {
		sources++
	}
	if len(c.Backends) > 0 {
		sources++
	}
	if c.Etcd != nil {
		sources++
	}
	if sources == 0 {
		return fmt.Errorf("one of backend, backends, or etcd must be set")
	}
	if sources > 1 {
		return fmt.Errorf("only one of backend, backends, or etcd may be set")
	}
	if c.Etcd != nil && c.Etcd.Prefix == "" {
		return fmt.Errorf("etcd.prefix is required")
	}
	if err := c.ACL.Validate(); err != nil {
		return err
	}
	return nil
}
