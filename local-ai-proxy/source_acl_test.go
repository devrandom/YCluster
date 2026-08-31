package main

import "testing"

func TestACLSnapshotCopiesOrNil(t *testing.T) {
	src := &EtcdSource{}
	if got := src.ACLSnapshot(); got != nil {
		t.Fatalf("empty source: want nil, got %#v", got)
	}

	src.aclModels.Store(&map[string]ACLModelRule{
		"model": {Entries: []ACLEntry{{Subject: "user:a", Decision: ACLAllow}}},
	})
	got := src.ACLSnapshot()
	if got == nil || len(got.Models) != 1 {
		t.Fatalf("want one model rule, got %#v", got)
	}

	// Mutating the snapshot must not affect the source snapshot.
	for k := range got.Models {
		delete(got.Models, k)
	}
	if len(*src.aclModels.Load()) != 1 {
		t.Fatal("source ACL was mutated by snapshot")
	}
}

func TestClearACLModels(t *testing.T) {
	src := &EtcdSource{}
	src.aclModels.Store(&map[string]ACLModelRule{"m": {}})
	src.clearACLModels()
	if src.ACLSnapshot() != nil {
		t.Fatal("cleared source: want nil ACL snapshot")
	}
}
