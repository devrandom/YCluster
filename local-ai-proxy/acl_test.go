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

func TestParseACLEntries(t *testing.T) {
	for _, tc := range []struct {
		name    string
		tokens  []string
		wantErr bool
	}{
		{name: "allow user and group", tokens: []string{"+user:alice", "+group:admins"}, wantErr: false},
		{name: "deny user", tokens: []string{"-user:bob"}, wantErr: false},
		{name: "deny group", tokens: []string{"-group:staff"}, wantErr: false},
		{name: "mixed tokens", tokens: []string{"+user:alice", "-user:bob", "+group:admins", "-group:staff"}, wantErr: false},
		{name: "wildcard user", tokens: []string{"+user:*"}, wantErr: false},
		{name: "wildcard group", tokens: []string{"-group:*"}, wantErr: false},
		{name: "invalid token", tokens: []string{"+user"}, wantErr: true},
		{name: "clear not a token", tokens: []string{"clear"}, wantErr: true},
		{name: "unknown prefix", tokens: []string{"xuser:alice"}, wantErr: true},
		{name: "empty user name", tokens: []string{"+user:"}, wantErr: true},
		{name: "concatenated tokens", tokens: []string{"+user:alice-user:*"}, wantErr: true},
		{name: "embedded user marker", tokens: []string{"+user:alice+user:bob"}, wantErr: true},
		{name: "whitespace in subject", tokens: []string{"+user:alice bob"}, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseACLEntries(tc.tokens)
			if tc.wantErr && err == nil {
				t.Errorf("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Errorf("unexpected error: %v", err)
			}
		})
	}
}

func TestParseACLEntriesOrder(t *testing.T) {
	got, err := ParseACLEntries([]string{"+user:alice", "-user:*"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []ACLEntry{
		{Subject: "user:alice", Decision: ACLAllow},
		{Subject: "user:*", Decision: ACLDeny},
	}
	if len(got) != len(want) {
		t.Fatalf("entries = %v; want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("entries[%d] = %v; want %v", i, got[i], want[i])
		}
	}
}

func TestACLConfigValidate(t *testing.T) {
	good := &ACLConfig{Models: map[string]ACLModelRule{
		"m": {Entries: []ACLEntry{
			{Subject: "user:alice", Decision: ACLAllow},
			{Subject: "group:staff", Decision: ACLDeny},
			{Subject: "user:*", Decision: ACLDeny},
		}},
	}}
	if err := good.Validate(); err != nil {
		t.Errorf("good config rejected: %v", err)
	}
	bad := &ACLConfig{Models: map[string]ACLModelRule{
		"m": {Entries: []ACLEntry{{Subject: "user:alice-user:*", Decision: ACLAllow}}},
	}}
	if err := bad.Validate(); err == nil {
		t.Errorf("expected error for concatenated subject")
	}
	var nilCfg *ACLConfig
	if err := nilCfg.Validate(); err != nil {
		t.Errorf("nil config should validate, got %v", err)
	}
}
