package main

import (
	"net/url"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// applyWatchEvents folds a batch of etcd watch events into the current
// snapshot. Copy-on-write keeps readers lock-free: they see either the old
// or the new map, never a partially applied batch.
func (s *EtcdSource) applyWatchEvents(events []*clientv3.Event) {
	var superseded map[string]string
	if cur := s.superseded.Load(); cur != nil {
		superseded = *cur
	}
	nextM, nextA, nextS := cloneSourceState(
		*s.snapshot.Load(), *s.aclModels.Load(), superseded, len(events))
	for _, ev := range events {
		s.applyEvent(nextM, nextA, nextS, ev)
	}
	s.snapshot.Store(&nextM)
	s.aclModels.Store(&nextA)
	s.superseded.Store(&nextS)
}

// cloneSourceState copies the routable models, ACL rules and superseded
// markers so a batch of events can be applied without mutating state that
// concurrent readers still hold.
func cloneSourceState(
	m map[string][]*url.URL,
	a map[string]ACLModelRule,
	superseded map[string]string,
	extra int,
) (map[string][]*url.URL, map[string]ACLModelRule, map[string]string) {
	nextM := make(map[string][]*url.URL, len(m)+extra)
	for k, v := range m {
		nextM[k] = v
	}
	nextA := make(map[string]ACLModelRule, len(a)+extra)
	for k, v := range a {
		nextA[k] = v
	}
	nextS := make(map[string]string, len(superseded)+extra)
	for k, v := range superseded {
		nextS[k] = v
	}
	return nextM, nextA, nextS
}

func (s *EtcdSource) applyEvent(
	nextM map[string][]*url.URL,
	nextA map[string]ACLModelRule,
	nextS map[string]string,
	ev *clientv3.Event,
) {
	name := s.modelName(ev.Kv.Key)
	switch ev.Type {
	case clientv3.EventTypePut:
		urls, acl, supersededBy, ok := s.parseKV(ev.Kv.Key, ev.Kv.Value)
		if ok {
			applyModelPut(nextM, nextA, nextS, name, urls, acl, supersededBy)
		}
	case clientv3.EventTypeDelete:
		delete(nextM, name)
		delete(nextA, name)
		delete(nextS, name)
	}
}

// applyModelPut records one put event. Superseded models are never routable,
// even when the entry still lists backends.
func applyModelPut(
	nextM map[string][]*url.URL,
	nextA map[string]ACLModelRule,
	nextS map[string]string,
	name string,
	urls []*url.URL,
	acl *ACLModelRule,
	supersededBy string,
) {
	if supersededBy != "" {
		delete(nextM, name)
		nextS[name] = supersededBy
	} else {
		if len(urls) > 0 {
			nextM[name] = urls
		} else {
			delete(nextM, name)
		}
		delete(nextS, name)
	}
	if acl != nil {
		nextA[name] = *acl
	} else {
		delete(nextA, name)
	}
}
