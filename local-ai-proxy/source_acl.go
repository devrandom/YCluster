package main

// ACLSnapshot returns the etcd-side ACL view (inline per-model rules
// plus the singleton default key, if configured). nil when no rules
// or default are present, so MergeACL can skip work entirely.
func (s *EtcdSource) ACLSnapshot() *ACLConfig {
	modelsCopy := s.copyACLModels()
	if modelsCopy == nil {
		return nil
	}
	return &ACLConfig{Models: modelsCopy}
}

// copyACLModels snapshots the per-model ACL rules, or nil when empty.
func (s *EtcdSource) copyACLModels() map[string]ACLModelRule {
	mp := s.aclModels.Load()
	if mp == nil || len(*mp) == 0 {
		return nil
	}
	out := make(map[string]ACLModelRule, len(*mp))
	for k, v := range *mp {
		out[k] = v
	}
	return out
}

// SupersededSnapshot returns the etcd-side obsolete → replacement view
// (inline superseded_by fields), or nil when empty.
func (s *EtcdSource) SupersededSnapshot() map[string]string {
	mp := s.superseded.Load()
	if mp == nil || len(*mp) == 0 {
		return nil
	}
	out := make(map[string]string, len(*mp))
	for k, v := range *mp {
		out[k] = v
	}
	return out
}

// clearACLModels removes all inline model rules (test helper seam).
func (s *EtcdSource) clearACLModels() {
	s.aclModels.Store(&map[string]ACLModelRule{})
}
