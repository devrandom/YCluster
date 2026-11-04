package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestAffinityKeyPrecedence(t *testing.T) {
	cases := []struct {
		name    string
		headers map[string]string
		bodyKey string
		want    string
	}{
		{
			name:    "OWUI chat id beats everything",
			headers: map[string]string{"X-OpenWebUI-Chat-Id": "chat-1", "Thread-Id": "t1", "X-User-Id": "u1"},
			bodyKey: "cache-1",
			want:    "owui-chat:chat-1",
		},
		{
			name:    "prompt_cache_key beats session headers",
			headers: map[string]string{"Thread-Id": "t1", "Session-Id": "s1"},
			bodyKey: "cache-1",
			want:    "cache-key:cache-1",
		},
		{
			name:    "thread header",
			headers: map[string]string{"Thread-Id": "t1", "Session-Id": "s1"},
			want:    "thread:t1",
		},
		{
			name:    "session header",
			headers: map[string]string{"Session-Id": "s1", "X-User-Id": "u1"},
			want:    "session:s1",
		},
		{
			name:    "per-request ids do not masquerade as conversations",
			headers: map[string]string{"X-Client-Request-Id": "r1", "X-User-Id": "u1"},
			want:    "user:u1",
		},
		{
			name:    "user id last resort",
			headers: map[string]string{"X-User-Id": "u1"},
			want:    "user:u1",
		},
		{
			name:    "blank header falls through",
			headers: map[string]string{"X-OpenWebUI-Chat-Id": "   "},
			bodyKey: "cache-1",
			want:    "cache-key:cache-1",
		},
		{
			name: "no identity at all",
			want: "",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
			for k, v := range tc.headers {
				req.Header.Set(k, v)
			}
			if got := affinityKey(req, tc.bodyKey); got != tc.want {
				t.Errorf("affinityKey() = %q; want %q", got, tc.want)
			}
		})
	}
}

func TestRendezvousPickIsStableAndSpread(t *testing.T) {
	candidates := []*url.URL{
		mustURL(t, "http://a.example:8000"),
		mustURL(t, "http://b.example:8000"),
		mustURL(t, "http://c.example:8000"),
	}

	for i := 0; i < 20; i++ {
		if got, again := rendezvousPick(candidates, "chat-1"), rendezvousPick(candidates, "chat-1"); got != again {
			t.Fatalf("rendezvousPick is not deterministic: %v then %v", got, again)
		}
	}

	const keys = 900
	counts := map[string]int{}
	for i := 0; i < keys; i++ {
		counts[rendezvousPick(candidates, fmt.Sprintf("chat-%d", i)).String()]++
	}
	for _, u := range candidates {
		if c := counts[u.String()]; c < keys/5 || c > keys/2 {
			t.Errorf("backend %s owns %d of %d keys; want roughly a third", u, c, keys)
		}
	}
}

func TestPickBackendAffinityRespectsSkew(t *testing.T) {
	a := mustURL(t, "http://a.example:8000")
	b := mustURL(t, "http://b.example:8000")
	candidates := []*url.URL{a, b}
	load := NewLoadCounter()
	policy := AffinityPolicy{Enabled: true, MaxSkew: 1}
	const key = "owui-chat:chat-42"

	pinned, outcome := PickBackendAffinity(candidates, load, key, policy)
	if outcome != AffinityPinned {
		t.Fatalf("outcome = %q; want %q", outcome, AffinityPinned)
	}
	if again, _ := PickBackendAffinity(candidates, load, key, policy); again != pinned {
		t.Fatalf("pinned backend moved between calls: %v then %v", pinned, again)
	}

	// One in-flight request on the pinned backend is still within the skew.
	load.Inc(pinned.String())
	if got, outcome := PickBackendAffinity(candidates, load, key, policy); got != pinned || outcome != AffinityPinned {
		t.Fatalf("with 1 in flight: got %v (%s); want %v (pinned)", got, outcome, pinned)
	}

	// Three in flight against an idle peer: drop the pin.
	load.Inc(pinned.String())
	load.Inc(pinned.String())
	got, outcome := PickBackendAffinity(candidates, load, key, policy)
	if outcome != AffinityRebalanced {
		t.Fatalf("outcome = %q; want %q", outcome, AffinityRebalanced)
	}
	if got == pinned {
		t.Fatalf("rebalanced onto the busy pinned backend %v", pinned)
	}
}

func TestPickBackendAffinityWithoutKeyOrPolicy(t *testing.T) {
	candidates := []*url.URL{
		mustURL(t, "http://a.example:8000"),
		mustURL(t, "http://b.example:8000"),
	}
	if _, outcome := PickBackendAffinity(candidates, NewLoadCounter(), "", AffinityPolicy{Enabled: true}); outcome != "" {
		t.Errorf("keyless request reported outcome %q; want none", outcome)
	}
	if _, outcome := PickBackendAffinity(candidates, NewLoadCounter(), "k", AffinityPolicy{}); outcome != "" {
		t.Errorf("disabled policy reported outcome %q; want none", outcome)
	}

	// A single candidate is always returned, keyed or not.
	one := candidates[:1]
	got, outcome := PickBackendAffinity(one, NewLoadCounter(), "k", AffinityPolicy{Enabled: true})
	if got != one[0] || outcome != "" {
		t.Errorf("single candidate: got %v (%q); want %v with no outcome", got, outcome, one[0])
	}
}

func TestModelRouterExtractsAffinityKey(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"m": {mustURL(t, "http://a.example:8000")},
	}}
	router := NewModelRouter(src)

	req := httptest.NewRequest(http.MethodPost, "/v1/responses",
		strings.NewReader(`{"model":"m","prompt_cache_key":"thread-1"}`))
	req.Header.Set("Content-Type", "application/json")
	got, err := router.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if got.AffinityKey != "cache-key:thread-1" {
		t.Errorf("AffinityKey = %q; want cache-key:thread-1", got.AffinityKey)
	}

	req = httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"m","prompt_cache_key":"thread-1"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-OpenWebUI-Chat-Id", "chat-9")
	got, err = router.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if got.AffinityKey != "owui-chat:chat-9" {
		t.Errorf("AffinityKey = %q; want owui-chat:chat-9", got.AffinityKey)
	}
}

// TestHandlerPinsConversationsToBackends drives the full handler: every
// conversation id must keep landing on one backend, and the ids must not
// all collapse onto a single backend.
func TestHandlerPinsConversationsToBackends(t *testing.T) {
	var hits [2]int
	newBackend := func(marker string, idx int) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			hits[idx]++
			_, _ = w.Write([]byte(marker))
		}))
	}
	a := newBackend("a", 0)
	defer a.Close()
	b := newBackend("b", 1)
	defer b.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"m": {mustURL(t, a.URL), mustURL(t, b.URL)},
	}}
	handler := NewHandler(NewModelRouter(src))
	handler.Load = NewLoadCounter()
	handler.Affinity = AffinityPolicy{Enabled: true, MaxSkew: DefaultAffinityMaxSkew}
	proxy := httptest.NewServer(handler)
	defer proxy.Close()

	servedBy := map[string]string{}
	for i := 0; i < 20; i++ {
		chatID := fmt.Sprintf("chat-%d", i)
		for turn := 0; turn < 2; turn++ {
			req, err := http.NewRequest(http.MethodPost, proxy.URL+"/v1/chat/completions",
				strings.NewReader(`{"model":"m"}`))
			if err != nil {
				t.Fatal(err)
			}
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-OpenWebUI-Chat-Id", chatID)
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			body := make([]byte, 1)
			_, _ = resp.Body.Read(body)
			resp.Body.Close()
			got := string(body)
			if want, ok := servedBy[chatID]; ok && want != got {
				t.Fatalf("conversation %s moved from %s to %s", chatID, want, got)
			}
			servedBy[chatID] = got
		}
	}
	if hits[0] == 0 || hits[1] == 0 {
		t.Fatalf("affinity collapsed onto one backend: hits = %v", hits)
	}
}
