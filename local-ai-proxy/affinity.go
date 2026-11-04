package main

import (
	"hash/fnv"
	"net/url"
)

// DefaultAffinityMaxSkew is how many more in-flight requests the
// affinity-pinned backend may carry than the least-loaded candidate
// before a request is rebalanced away. 1 keeps a conversation on its
// node while a single long generation is running there, but lets a
// pile-up move new requests elsewhere. Set 0 to pin only while the
// backend is not busier than any peer.
const DefaultAffinityMaxSkew = 1

// AffinityPolicy controls conversation stickiness. The zero value
// disables it; configureHandler installs the policy from the YAML
// config.
type AffinityPolicy struct {
	Enabled bool
	MaxSkew int
}

// Affinity outcomes reported by PickBackendAffinity and the affinity
// metric.
const (
	AffinityPinned     = "pinned"
	AffinityRebalanced = "rebalanced"
)

// PickBackendAffinity selects a backend like PickBackend, but a request
// carrying a conversation key is pinned to the backend rendezvous
// hashing assigns to that key, so the engine's prefix cache stays warm
// across turns. The pin is dropped when the pinned backend already runs
// MaxSkew more requests than the least-loaded candidate.
//
// outcome is AffinityPinned or AffinityRebalanced, or "" when there was
// no key (or affinity is off) and the choice was load-only.
func PickBackendAffinity(candidates []*url.URL, load Load, key string, policy AffinityPolicy) (*url.URL, string) {
	if len(candidates) == 0 {
		return nil, ""
	}
	if len(candidates) == 1 || key == "" || !policy.Enabled {
		return PickBackend(candidates, load), ""
	}

	pinned := rendezvousPick(candidates, key)
	if load == nil {
		return pinned, AffinityPinned
	}

	minCount := int64(-1)
	for _, u := range candidates {
		if c := load.Count(u.String()); minCount < 0 || c < minCount {
			minCount = c
		}
	}
	if load.Count(pinned.String()) <= minCount+int64(policy.MaxSkew) {
		return pinned, AffinityPinned
	}
	return leastLoaded(candidates, load), AffinityRebalanced
}

// rendezvousPick returns the candidate with the highest hash of
// key+url. Rendezvous (highest-random-weight) hashing keeps a key on
// its backend while the candidate set is stable and only moves the keys
// owned by a backend that disappears, which matters when the set
// changes because a node is added or goes unhealthy.
func rendezvousPick(candidates []*url.URL, key string) *url.URL {
	best := candidates[0]
	var bestScore uint64
	for i, u := range candidates {
		h := fnv.New64a()
		_, _ = h.Write([]byte(key))
		_, _ = h.Write([]byte{0})
		_, _ = h.Write([]byte(u.String()))
		if score := h.Sum64(); i == 0 || score > bestScore {
			best, bestScore = u, score
		}
	}
	return best
}
