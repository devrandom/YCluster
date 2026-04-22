package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Config is the top-level YAML schema. Shaped as a single-backend config
// today; `backend:` becomes `backends: []Backend` when multi-backend
// routing lands.
type Config struct {
	Listen  string  `yaml:"listen"`
	Backend Backend `yaml:"backend"`
}

type Backend struct {
	URL string `yaml:"url"`
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
	if c.Backend.URL == "" {
		return fmt.Errorf("backend.url is required")
	}
	return nil
}
