package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

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
}

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
	return nil
}
