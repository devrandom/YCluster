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

// MergeACL returns the union of base and overlay. If both are nil the
// result is nil (no ACL). If only one is nil the other is returned
// as-is. When both are present:
//
//   - Default = overlay.Default if non-empty, else base.Default.
//   - For each model present in either side, the result's user and
//     group lists are the union (deduped) of the two sides. So a rule
//     in either source broadens access; deletions on either side don't
//     narrow access while the other side still grants it.
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
		Default: base.Default,
		Models:  make(map[string]ACLModelRule, len(base.Models)+len(overlay.Models)),
	}
	if overlay.Default != "" {
		out.Default = overlay.Default
	}
	for m, r := range base.Models {
		out.Models[m] = r
	}
	for m, ov := range overlay.Models {
		if existing, ok := out.Models[m]; ok {
			out.Models[m] = ACLModelRule{
				Users:  unionStrings(existing.Users, ov.Users),
				Groups: unionStrings(existing.Groups, ov.Groups),
			}
		} else {
			out.Models[m] = ov
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
