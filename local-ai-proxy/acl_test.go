package main

import "testing"

func TestACLNilAlwaysAllows(t *testing.T) {
	var a *ACLConfig
	if err := a.Check("any", "", nil); err != nil {
		t.Errorf("nil ACL should allow, got %v", err)
	}
}

func TestACLDefaultAllow(t *testing.T) {
	a := &ACLConfig{Default: "allow"}
	if err := a.Check("anything", "alice", nil); err != nil {
		t.Errorf("default=allow + no rule should allow, got %v", err)
	}
}

func TestACLDefaultEmptyTreatedAsAllow(t *testing.T) {
	a := &ACLConfig{}
	if err := a.Check("anything", "alice", nil); err != nil {
		t.Errorf("empty default should allow, got %v", err)
	}
}

func TestACLDefaultDeny(t *testing.T) {
	a := &ACLConfig{Default: "deny"}
	if err := a.Check("anything", "alice", []string{"staff"}); err == nil {
		t.Errorf("default=deny + no rule should deny")
	}
}

func TestACLUserMatch(t *testing.T) {
	a := &ACLConfig{
		Default: "deny",
		Models: map[string]ACLModelRule{
			"m": {Users: []string{"alice"}},
		},
	}
	if err := a.Check("m", "alice", nil); err != nil {
		t.Errorf("user match should allow, got %v", err)
	}
	if err := a.Check("m", "bob", nil); err == nil {
		t.Errorf("non-matching user should deny")
	}
}

func TestACLGroupMatch(t *testing.T) {
	a := &ACLConfig{
		Default: "deny",
		Models: map[string]ACLModelRule{
			"m": {Groups: []string{"admins"}},
		},
	}
	if err := a.Check("m", "alice", []string{"staff", "admins"}); err != nil {
		t.Errorf("group member should allow, got %v", err)
	}
	if err := a.Check("m", "alice", []string{"staff"}); err == nil {
		t.Errorf("non-member should deny")
	}
}

func TestACLWildcardUser(t *testing.T) {
	a := &ACLConfig{
		Default: "deny",
		Models: map[string]ACLModelRule{
			"m": {Users: []string{"*"}},
		},
	}
	if err := a.Check("m", "anyone", nil); err != nil {
		t.Errorf("user=* should allow any authenticated, got %v", err)
	}
}

func TestACLWildcardGroup(t *testing.T) {
	a := &ACLConfig{
		Default: "deny",
		Models: map[string]ACLModelRule{
			"m": {Groups: []string{"*"}},
		},
	}
	if err := a.Check("m", "alice", nil); err != nil {
		t.Errorf("group=* should allow even with no groups, got %v", err)
	}
}

func TestACLDefaultAllowWithRuleStillEnforced(t *testing.T) {
	// A rule on a model overrides default=allow: matching it requires
	// the user/group lists to permit, otherwise denied.
	a := &ACLConfig{
		Default: "allow",
		Models: map[string]ACLModelRule{
			"m": {Users: []string{"alice"}},
		},
	}
	if err := a.Check("m", "bob", nil); err == nil {
		t.Errorf("rule should deny non-matching user even when default=allow")
	}
	if err := a.Check("other", "bob", nil); err != nil {
		t.Errorf("unlisted model should follow default=allow, got %v", err)
	}
}

func TestACLValidate(t *testing.T) {
	for _, tc := range []struct {
		name    string
		def     string
		wantErr bool
	}{
		{"empty", "", false},
		{"allow", "allow", false},
		{"deny", "deny", false},
		{"bogus", "maybe", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := (&ACLConfig{Default: tc.def}).Validate()
			if tc.wantErr && err == nil {
				t.Errorf("want error for default=%q", tc.def)
			}
			if !tc.wantErr && err != nil {
				t.Errorf("unexpected error: %v", err)
			}
		})
	}
}

func TestMergeACLNilCases(t *testing.T) {
	if got := MergeACL(nil, nil); got != nil {
		t.Errorf("nil/nil = %v; want nil", got)
	}
	a := &ACLConfig{Default: "deny"}
	if got := MergeACL(a, nil); got != a {
		t.Errorf("a/nil should return a unchanged")
	}
	if got := MergeACL(nil, a); got != a {
		t.Errorf("nil/a should return a unchanged")
	}
}

func TestMergeACLDefaults(t *testing.T) {
	cases := []struct {
		base, overlay, want string
	}{
		{"allow", "", "allow"},
		{"deny", "", "deny"},
		{"allow", "deny", "deny"},
		{"deny", "allow", "allow"},
		{"", "deny", "deny"},
	}
	for _, c := range cases {
		got := MergeACL(&ACLConfig{Default: c.base}, &ACLConfig{Default: c.overlay})
		if got.Default != c.want {
			t.Errorf("merge(%q,%q).Default = %q; want %q", c.base, c.overlay, got.Default, c.want)
		}
	}
}

func TestMergeACLUnionsRules(t *testing.T) {
	base := &ACLConfig{
		Default: "deny",
		Models: map[string]ACLModelRule{
			"shared": {Users: []string{"alice"}, Groups: []string{"staff"}},
			"only-base": {Users: []string{"bob"}},
		},
	}
	overlay := &ACLConfig{
		Models: map[string]ACLModelRule{
			"shared": {Users: []string{"carol", "alice"}, Groups: []string{"admins"}},
			"only-overlay": {Groups: []string{"hackers"}},
		},
	}
	merged := MergeACL(base, overlay)

	if got := merged.Default; got != "deny" {
		t.Errorf("Default = %q; want deny (from base)", got)
	}
	// shared should have union of both
	shared := merged.Models["shared"]
	wantU := []string{"alice", "carol"}
	if !sameSet(shared.Users, wantU) {
		t.Errorf("shared.Users = %v; want set %v", shared.Users, wantU)
	}
	wantG := []string{"staff", "admins"}
	if !sameSet(shared.Groups, wantG) {
		t.Errorf("shared.Groups = %v; want set %v", shared.Groups, wantG)
	}
	// only-base passes through
	if !sameSet(merged.Models["only-base"].Users, []string{"bob"}) {
		t.Errorf("only-base lost: %v", merged.Models["only-base"])
	}
	// only-overlay passes through
	if !sameSet(merged.Models["only-overlay"].Groups, []string{"hackers"}) {
		t.Errorf("only-overlay lost: %v", merged.Models["only-overlay"])
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
