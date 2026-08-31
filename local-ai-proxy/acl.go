package main

import (
	"fmt"
	"strings"
)

type ACLConfig struct {
	Models map[string]ACLModelRule `yaml:"models,omitempty"`
}

type ACLDecision bool

const (
	ACLAllow ACLDecision = true
	ACLDeny  ACLDecision = false
)

type ACLEntry struct {
	Subject  string      `yaml:"subject"`
	Decision ACLDecision `yaml:"decision"`
}

type ACLModelRule struct {
	Entries []ACLEntry `yaml:"entries,omitempty"`
}

func (a *ACLConfig) Validate() error {
	if a == nil {
		return nil
	}
	for model, rule := range a.Models {
		for i, e := range rule.Entries {
			if err := validateSubject(e.Subject); err != nil {
				return fmt.Errorf("model %q entry %d: %v", model, i, err)
			}
		}
	}
	return nil
}

// validateSubject rejects malformed subject strings. Catches typos
// ("users:alice"), empty names ("user:"), and concatenation bugs
// where multiple CLI tokens got squashed into one ("user:a-user:*").
func validateSubject(s string) error {
	var name string
	switch {
	case strings.HasPrefix(s, "user:"):
		name = strings.TrimPrefix(s, "user:")
	case strings.HasPrefix(s, "group:"):
		name = strings.TrimPrefix(s, "group:")
	default:
		return fmt.Errorf("subject %q must start with user: or group:", s)
	}
	if name == "" {
		return fmt.Errorf("subject %q has empty name", s)
	}
	if strings.ContainsAny(name, " \t\n\r") {
		return fmt.Errorf("subject %q contains whitespace", s)
	}
	for _, marker := range []string{"+user:", "-user:", "+group:", "-group:", "user:", "group:"} {
		if strings.Contains(name, marker) {
			return fmt.Errorf("subject %q contains embedded %q (likely concatenated tokens)", s, marker)
		}
	}
	return nil
}

func (a *ACLConfig) Check(model, user string, groups []string) error {
	if a == nil {
		return nil
	}
	rule, ok := a.Models[model]
	if !ok {
		return nil
	}
	for _, e := range rule.Entries {
		if !entryMatches(e.Subject, user, groups) {
			continue
		}
		if e.Decision == ACLDeny {
			return aclDenied(model)
		}
		return nil
	}
	return nil
}

func entryMatches(subject, user string, groups []string) bool {
	if strings.HasPrefix(subject, "user:") {
		u := strings.TrimPrefix(subject, "user:")
		return u == "*" || u == user
	}
	if strings.HasPrefix(subject, "group:") {
		g := strings.TrimPrefix(subject, "group:")
		if g == "*" {
			return true
		}
		for _, ug := range groups {
			if g == ug {
				return true
			}
		}
	}
	return false
}

// SplitGroups parses the comma-separated X-User-Groups header into a
// slice, trimming whitespace and dropping empty entries.
func SplitGroups(header string) []string {
	if header == "" {
		return nil
	}
	parts := strings.Split(header, ",")
	out := parts[:0]
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func aclDenied(model string) error {
	return fmt.Errorf("model %q is not permitted for this user", model)
}

// ParseACLEntries turns CLI tokens like "+user:alice" / "-group:staff"
// into ACLEntry values, preserving order.
func appendEntries(a, b []ACLEntry) []ACLEntry {
	seen := make(map[string]bool, len(a)+len(b))
	var out []ACLEntry
	for _, e := range a {
		if !seen[e.Subject] {
			seen[e.Subject] = true
			out = append(out, e)
		}
	}
	for _, e := range b {
		if !seen[e.Subject] {
			seen[e.Subject] = true
			out = append(out, e)
		}
	}
	return out
}
