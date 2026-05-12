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

// Config is the top-level YAML schema. Exactly one of backend, backends,
// or etcd must be set.
type Config struct {
	Listen string `yaml:"listen"`

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

	// ACLDefaultKey is the etcd singleton key whose value ("allow" or
	// "deny") overrides the YAML acl.default at runtime. Empty disables
	// the override (YAML default is authoritative). The proxy watches
	// this key for hot-reload.
	ACLDefaultKey string `yaml:"acl_default_key,omitempty"`
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
