package main

import "testing"

func TestACLNilAlwaysAllows(t *testing.T) {
	var a *ACLConfig
	if err := a.Check("any", "", nil); err != nil {
		t.Errorf("nil ACL should allow, got %v", err)
	}
}

func TestACLUserMatch(t *testing.T) {
	a := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{
				{Subject: "user:alice", Decision: ACLAllow},
				{Subject: "user:*", Decision: ACLDeny},
			}},
		},
	}
	if err := a.Check("m", "alice", nil); err != nil {
		t.Errorf("user match should allow, got %v", err)
	}
	if err := a.Check("m", "bob", nil); err == nil {
		t.Errorf("non-matching user should deny by user:*")
	}
}

func TestACLGroupMatch(t *testing.T) {
	a := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{
				{Subject: "group:admins", Decision: ACLAllow},
				{Subject: "group:*", Decision: ACLDeny},
			}},
		},
	}
	if err := a.Check("m", "alice", []string{"staff", "admins"}); err != nil {
		t.Errorf("group member should allow, got %v", err)
	}
	if err := a.Check("m", "alice", []string{"staff"}); err == nil {
		t.Errorf("non-member should deny by group:*")
	}
}

func TestACLWildcardUser(t *testing.T) {
	a := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{{Subject: "user:*", Decision: ACLAllow}}},
		},
	}
	if err := a.Check("m", "anyone", nil); err != nil {
		t.Errorf("user=* should allow any authenticated, got %v", err)
	}
}

func TestACLWildcardUserEmptyUser(t *testing.T) {
	// user:* matches even when no X-User-Id header was supplied
	// (handler.go passes "" in that case). Document the behavior: the
	// wildcard means "any subject", not "any authenticated subject".
	allow := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{{Subject: "user:*", Decision: ACLAllow}}},
		},
	}
	if err := allow.Check("m", "", nil); err != nil {
		t.Errorf("+user:* should allow empty user, got %v", err)
	}
	deny := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{{Subject: "user:*", Decision: ACLDeny}}},
		},
	}
	if err := deny.Check("m", "", nil); err == nil {
		t.Errorf("-user:* should deny empty user")
	}
}

func TestACLWildcardGroup(t *testing.T) {
	a := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{{Subject: "group:*", Decision: ACLAllow}}},
		},
	}
	if err := a.Check("m", "alice", nil); err != nil {
		t.Errorf("group=* should allow even with no groups, got %v", err)
	}
}

func TestACLAllowSpecificThenDenyAll(t *testing.T) {
	a := &ACLConfig{
		Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{
				{Subject: "user:x@y.com", Decision: ACLAllow},
				{Subject: "user:*", Decision: ACLDeny},
			}},
		},
	}
	if err := a.Check("m", "x@y.com", nil); err != nil {
		t.Errorf("x@y.com should be allowed, got %v", err)
	}
	if err := a.Check("m", "anyone@else.com", nil); err == nil {
		t.Errorf("anyone@else.com should be denied by user:* rule")
	}
}

func TestMergeACLNilCases(t *testing.T) {
	if got := MergeACL(nil, nil); got != nil {
		t.Errorf("nil/nil = %v; want nil", got)
	}
	a := &ACLConfig{Models: map[string]ACLModelRule{"m": {}}}
	if got := MergeACL(a, nil); got != a {
		t.Errorf("a/nil should return a unchanged")
	}
	if got := MergeACL(nil, a); got != a {
		t.Errorf("nil/a should return a unchanged")
	}
}

func TestMergeACLUnionsRules(t *testing.T) {
	base := &ACLConfig{
		Models: map[string]ACLModelRule{
			"shared": {Entries: []ACLEntry{
				{Subject: "user:alice", Decision: ACLAllow},
				{Subject: "group:staff", Decision: ACLAllow},
			}},
			"only-base": {Entries: []ACLEntry{{Subject: "user:bob", Decision: ACLAllow}}},
		},
	}
	overlay := &ACLConfig{
		Models: map[string]ACLModelRule{
			"shared": {Entries: []ACLEntry{
				{Subject: "user:carol", Decision: ACLAllow},
				{Subject: "group:admins", Decision: ACLAllow},
			}},
			"only-overlay": {Entries: []ACLEntry{{Subject: "group:hackers", Decision: ACLAllow}}},
		},
	}
	merged := MergeACL(base, overlay)

	// shared should have union of both
	shared := merged.Models["shared"]
	subjects := make(map[string]bool)
	for _, e := range shared.Entries {
		subjects[e.Subject] = true
	}
	want := []string{"user:alice", "group:staff", "user:carol", "group:admins"}
	if len(subjects) != len(want) {
		t.Errorf("shared.Entries count = %d; want %d", len(subjects), len(want))
	}
	// only-base passes through
	onlyBase := merged.Models["only-base"]
	if len(onlyBase.Entries) != 1 || onlyBase.Entries[0].Subject != "user:bob" {
		t.Errorf("only-base lost: %v", onlyBase)
	}
	// only-overlay passes through
	onlyOverlay := merged.Models["only-overlay"]
	if len(onlyOverlay.Entries) != 1 || onlyOverlay.Entries[0].Subject != "group:hackers" {
		t.Errorf("only-overlay lost: %v", onlyOverlay)
	}
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	seen := make(map[string]struct{}, len(a))
	for _, s := range a {
		seen[s] = struct{}{}
	}
	for _, s := range b {
		if _, ok := seen[s]; !ok {
			return false
		}
	}
	return true
}

func TestSplitGroups(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want []string
	}{
		{"", nil},
		{"a", []string{"a"}},
		{"a,b,c", []string{"a", "b", "c"}},
		{" a , b ,, c ", []string{"a", "b", "c"}},
	} {
		got := SplitGroups(tc.in)
		if len(got) != len(tc.want) {
			t.Errorf("SplitGroups(%q) = %v; want %v", tc.in, got, tc.want)
			continue
		}
		for i := range got {
			if got[i] != tc.want[i] {
				t.Errorf("SplitGroups(%q) = %v; want %v", tc.in, got, tc.want)
				break
			}
		}
	}
}

func TestParseACLDeltas(t *testing.T) {
	for _, tc := range []struct {
		name    string
		tokens  []string
		wantErr bool
	}{
		{
			name:    "allow user and group",
			tokens:  []string{"+user:alice", "+group:admins"},
			wantErr: false,
		},
		{
			name:    "deny user",
			tokens:  []string{"-user:bob"},
			wantErr: false,
		},
		{
			name:    "deny group",
			tokens:  []string{"-group:staff"},
			wantErr: false,
		},
		{
			name:    "mixed tokens",
			tokens:  []string{"+user:alice", "-user:bob", "+group:admins", "-group:staff"},
			wantErr: false,
		},
		{
			name:    "invalid token",
			tokens:  []string{"+user"},
			wantErr: true,
		},
		{
			name:    "clear not a token",
			tokens:  []string{"clear"},
			wantErr: true,
		},
		{
			name:    "unknown prefix",
			tokens:  []string{"xuser:alice"},
			wantErr: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseACLDeltas(tc.tokens)
			if tc.wantErr && err == nil {
				t.Errorf("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Errorf("unexpected error: %v", err)
			}
		})
	}
}

func TestACLDeltaApply(t *testing.T) {
	base := ACLModelRule{
		Entries: []ACLEntry{
			{Subject: "user:alice", Decision: ACLAllow},
			{Subject: "group:staff", Decision: ACLAllow},
		},
	}
	d := ACLDelta{
		Entries: []ACLEntry{
			{Subject: "user:bob", Decision: ACLAllow},
			{Subject: "group:admins", Decision: ACLDeny},
		},
	}
	result := d.Apply(base)
	if len(result.Entries) != 2 {
		t.Errorf("Entries count = %d; want 2", len(result.Entries))
	}
	if result.Entries[0].Subject != "user:bob" || result.Entries[1].Subject != "group:admins" {
		t.Errorf("Entries = %v; want bob+admins", result.Entries)
	}
}

func TestACLDeltaApplyToEmpty(t *testing.T) {
	d := ACLDelta{
		Entries: []ACLEntry{
			{Subject: "user:x@y.com", Decision: ACLAllow},
			{Subject: "user:*", Decision: ACLDeny},
		},
	}
	result := d.Apply(ACLModelRule{})
	if len(result.Entries) != 2 {
		t.Errorf("Entries count = %d; want 2", len(result.Entries))
	}
	if result.Entries[0].Subject != "user:x@y.com" {
		t.Errorf("first entry = %v; want user:x@y.com", result.Entries[0].Subject)
	}
}

func TestACLDeltaApplyIdempotent(t *testing.T) {
	d := ACLDelta{
		Entries: []ACLEntry{{Subject: "user:alice", Decision: ACLAllow}},
	}
	rule := ACLModelRule{}
	r1 := d.Apply(rule)
	r2 := d.Apply(r1)
	if len(r2.Entries) != 1 {
		t.Errorf("should not duplicate: %v", r2.Entries)
	}
}
