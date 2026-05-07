package main

import (
	"fmt"
	"strings"
)

// ACLConfig is per-model access control. Default policy applies to
// any model with no explicit rule. A rule grants access if the
// authenticated user matches the user list OR is a member of any
// listed group. The literal "*" in either list means "any
// authenticated identity".
//
//	acl:
//	  default: allow      # or "deny"; applies to unlisted models
//	  models:
//	    secret-model:
//	      users: [root]
//	      groups: [admins]
//	    public-model:
//	      groups: ["*"]   # any authenticated user
type ACLConfig struct {
	Default string                  `yaml:"default,omitempty"`
	Models  map[string]ACLModelRule `yaml:"models,omitempty"`
}

type ACLModelRule struct {
	Users  []string `yaml:"users,omitempty"`
	Groups []string `yaml:"groups,omitempty"`
}

// Validate rejects unknown defaults early. An empty default is treated
// as "allow" for backwards compatibility with configs that pre-date ACLs.
func (a *ACLConfig) Validate() error {
	if a == nil {
		return nil
	}
	switch a.Default {
	case "", "allow", "deny":
	default:
		return fmt.Errorf("acl.default: must be \"allow\" or \"deny\", got %q", a.Default)
	}
	return nil
}

// Check returns nil when the request is permitted. Otherwise it
// returns a permission_denied error suitable for surfacing to the
// client. user may be empty (no X-User-Id); in that case only rules
// containing "*" or explicitly listing "" can match.
func (a *ACLConfig) Check(model, user string, groups []string) error {
	if a == nil {
		return nil
	}
	rule, ok := a.Models[model]
	if !ok {
		if a.Default == "deny" {
			return aclDenied(model)
		}
		return nil
	}
	for _, u := range rule.Users {
		if u == "*" || u == user {
			return nil
		}
	}
	for _, g := range rule.Groups {
		if g == "*" {
			return nil
		}
		for _, ug := range groups {
			if g == ug {
				return nil
			}
		}
	}
	return aclDenied(model)
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
