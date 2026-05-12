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
    Subject  string       `yaml:"subject"`
    Decision ACLDecision `yaml:"decision"`
}

type ACLModelRule struct {
    Entries []ACLEntry `yaml:"entries,omitempty"`
}

func (a *ACLConfig) Validate() error {
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

type ACLDelta struct {
	Entries []ACLEntry
}

func (d *ACLDelta) Apply(rule ACLModelRule) ACLModelRule {
	return ACLModelRule{Entries: d.Entries}
}

func ParseACLDeltas(tokens []string) ([]ACLDelta, error) {
	var delta ACLDelta
	for _, tok := range tokens {
		tok = strings.TrimSpace(tok)
		if tok == "" {
			continue
		}
		if strings.HasPrefix(tok, "+user:") {
			delta.Entries = append(delta.Entries, ACLEntry{
				Subject:  "user:" + strings.TrimPrefix(tok, "+user:"),
				Decision: ACLAllow,
			})
		} else if strings.HasPrefix(tok, "-user:") {
			delta.Entries = append(delta.Entries, ACLEntry{
				Subject:  "user:" + strings.TrimPrefix(tok, "-user:"),
				Decision: ACLDeny,
			})
		} else if strings.HasPrefix(tok, "+group:") {
			delta.Entries = append(delta.Entries, ACLEntry{
				Subject:  "group:" + strings.TrimPrefix(tok, "+group:"),
				Decision: ACLAllow,
			})
		} else if strings.HasPrefix(tok, "-group:") {
			delta.Entries = append(delta.Entries, ACLEntry{
				Subject:  "group:" + strings.TrimPrefix(tok, "-group:"),
				Decision: ACLDeny,
			})
		} else {
			return nil, fmt.Errorf("invalid ACL token %q", tok)
		}
	}
	if len(delta.Entries) > 0 {
		return []ACLDelta{delta}, nil
	}
	return nil, nil
}

func contains(slice []string, s string) bool {
	for _, v := range slice {
		if v == s {
			return true
		}
	}
	return false
}

func filterStrings(slice []string, reject string) []string {
	out := slice[:0]
	for _, s := range slice {
		if s != reject {
			out = append(out, s)
		}
	}
	return out
}

func MergeACL(base, overlay *ACLConfig) *ACLConfig {
	if base == nil && overlay == nil {
		return nil
	}
	if base == nil {
		return overlay
	}
	if overlay == nil {
		return base
	}
	out := &ACLConfig{
		Models: make(map[string]ACLModelRule, len(base.Models)+len(overlay.Models)),
	}
	for m, r := range base.Models {
		out.Models[m] = r
	}
	for m, ov := range overlay.Models {
		if existing, ok := out.Models[m]; ok {
			merged := appendEntries(existing.Entries, ov.Entries)
			out.Models[m] = ACLModelRule{Entries: merged}
		} else {
			out.Models[m] = ov
		}
	}
	return out
}

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

func unionStrings(a, b []string) []string {
	if len(a) == 0 {
		return append([]string(nil), b...)
	}
	if len(b) == 0 {
		return append([]string(nil), a...)
	}
	seen := make(map[string]struct{}, len(a)+len(b))
	out := make([]string, 0, len(a)+len(b))
	for _, s := range a {
		if _, ok := seen[s]; !ok {
			seen[s] = struct{}{}
			out = append(out, s)
		}
	}
	for _, s := range b {
		if _, ok := seen[s]; !ok {
			seen[s] = struct{}{}
			out = append(out, s)
		}
	}
	return out
}
