package main

import (
	"fmt"
	"strings"
)

func ParseACLEntries(tokens []string) ([]ACLEntry, error) {
	var out []ACLEntry
	for _, tok := range tokens {
		tok = strings.TrimSpace(tok)
		if tok == "" {
			continue
		}
		var (
			subject  string
			decision ACLDecision
		)
		switch {
		case strings.HasPrefix(tok, "+user:"):
			subject = "user:" + strings.TrimPrefix(tok, "+user:")
			decision = ACLAllow
		case strings.HasPrefix(tok, "-user:"):
			subject = "user:" + strings.TrimPrefix(tok, "-user:")
			decision = ACLDeny
		case strings.HasPrefix(tok, "+group:"):
			subject = "group:" + strings.TrimPrefix(tok, "+group:")
			decision = ACLAllow
		case strings.HasPrefix(tok, "-group:"):
			subject = "group:" + strings.TrimPrefix(tok, "-group:")
			decision = ACLDeny
		default:
			return nil, fmt.Errorf("invalid ACL token %q", tok)
		}
		if err := validateSubject(subject); err != nil {
			return nil, err
		}
		out = append(out, ACLEntry{Subject: subject, Decision: decision})
	}
	return out, nil
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

// appendEntries unions entries from a (base) and b (overlay), keyed by
// Subject. The first occurrence wins, so base entries take precedence
// over conflicting overlay entries with the same Subject. This is
// deliberate: YAML-configured rules should not be silently overridden
// by etcd writes.
