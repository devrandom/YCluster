package main

import (
	"bytes"
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// publishNamespaceMap writes the file the root-side enumerator produces and
// points the forwarder at it. Each namespace carries every cgroup text the
// publisher saw inside it.
func publishNamespaceMap(t *testing.T, entries map[string][]string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "namespaces.json")
	body, err := json.Marshal(entries)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, body, 0o644); err != nil {
		t.Fatal(err)
	}
	old := namespaceMapPath
	namespaceMapPath = path
	resetNamespaceMap()
	t.Cleanup(func() {
		namespaceMapPath = old
		resetNamespaceMap()
	})
}

func TestNamespaceMapNamesADeadProcess(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{
		"4026532001": {"0::/lxc.payload.tenant/init.scope\n"},
	})

	got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS())
	if !got.Resolved || got.Layer != "container" || got.Name != "tenant" || got.Source != "namespace+nsmap" {
		t.Fatalf("layer = %+v, want the container from the published namespace", got)
	}
}

func TestNamespaceMapRejectsWhatIsNotAContainer(t *testing.T) {
	cases := []struct {
		name   string
		cgroup string
	}{
		{"host cgroup", "0::/user.slice/user-1000.slice/session-3.scope\n"},
		// A rootless runtime belongs to a login user: the forwarder keeps those
		// unverified rather than naming them as a managed container.
		{"user scope runtime", "0::/user.slice/user-1000.slice/user@1000.service/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope\n"},
	}
	for _, tc := range cases {
		withProcRoot(t, t.TempDir())
		publishNamespaceMap(t, map[string][]string{"4026532001": {tc.cgroup}})
		got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS())
		if got.Layer != "unverified" {
			t.Fatalf("%s: layer = %+v, want unverified", tc.name, got)
		}
	}
}

// A container runs a service per cgroup, so the publisher hands over several
// texts for one namespace. They all name the same container, which is the
// answer.
func TestNamespaceMapAcceptsSeveralCgroupsOfOneContainer(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{
		"4026532001": {
			"0::/lxc.payload.tenant/init.scope\n",
			"0::/lxc.payload.tenant/system.slice/ssh.service\n",
			"0::/lxc.payload.tenant/user.slice/user-1000.slice/user@1000.service/init.scope\n",
		},
	})

	got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS())
	if !got.Resolved || got.Layer != "container" || got.Name != "tenant" || got.Source != "namespace+nsmap" {
		t.Fatalf("layer = %+v, want the container every text names", got)
	}
}

// A namespace shared by two containers has no single owner: naming either one
// would misattribute the other's events. The refusal must not cost the layer
// verdict, though: an idmapped uid still says "container", just unnamed, and
// a login uid still says unverified.
func TestNamespaceMapRefusesTwoOwners(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{
		"4026532001": {
			"0::/lxc.payload.tenant/init.scope\n",
			"0::/lxc.payload.other/init.scope\n",
		},
	})

	got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000123, containerNS())
	if !got.Resolved || got.Layer != "container" || got.Name != "" || got.Source != "namespace+uid" {
		t.Fatalf("layer = %+v, want an unnamed container from the uid heuristic", got)
	}
	if got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS()); got.Layer != "unverified" {
		t.Fatalf("layer = %+v, want unverified for a login uid in a shared namespace", got)
	}
}

func TestNamespaceMapRefusesAMapOverItsSizeLimit(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{
		"4026532001": {"0::/lxc.payload.tenant/init.scope\n"},
	})
	// Valid JSON over the limit. A corrupt file would fail the parse whether
	// the limit exists or not, so the payload has to be a map the reader
	// could otherwise use. The reader enforces the limit twice (a stat gate
	// and a bounded read), and this test is about the property, not about
	// either one of them.
	body, err := json.Marshal(map[string][]string{
		"4026532001": {strings.Repeat("x", namespaceMapMaxBytes)},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(body) <= namespaceMapMaxBytes {
		t.Fatalf("test payload is %d bytes, not over the limit", len(body))
	}
	if err := os.WriteFile(namespaceMapPath, body, 0o644); err != nil {
		t.Fatal(err)
	}
	resetNamespaceMap()
	if _, ok := namespaceMapLookup(4026532001); ok {
		t.Fatal("an oversized map must read as empty")
	}
}

// The file is re-read every namespaceMapTTL: a persistent problem must not
// write a line every ten seconds.
func TestNamespaceMapProblemIsReportedOnce(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{
		"4026532001": {"0::/lxc.payload.tenant/init.scope\n"},
	})
	if err := os.WriteFile(namespaceMapPath, []byte("{\"4026532001\": \"old format\"}"), 0o644); err != nil {
		t.Fatal(err)
	}

	var logged bytes.Buffer
	log.SetOutput(&logged)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })

	for i := 0; i < 3; i++ {
		resetNamespaceMap()
		if _, ok := namespaceMapLookup(4026532001); ok {
			t.Fatal("an unparseable map must read as empty")
		}
	}
	if got := strings.Count(logged.String(), "unreadable"); got != 1 {
		t.Fatalf("logged %d times, want once: %q", got, logged.String())
	}
}

func TestNamespaceMapIgnoresAnUnknownNamespace(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{"4026532999": {"0::/lxc.payload.other/init.scope\n"}})
	if got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS()); got.Layer != "unverified" {
		t.Fatalf("layer = %+v, want unverified for a namespace that is not published", got)
	}
}

func TestNamespaceMapExpiresWhenThePublisherStops(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string][]string{"4026532001": {"0::/lxc.payload.tenant/init.scope\n"}})
	stale := time.Now().Add(-2 * namespaceMapMaxAge)
	if err := os.Chtimes(namespaceMapPath, stale, stale); err != nil {
		t.Fatal(err)
	}
	resetNamespaceMap()
	if got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS()); got.Layer != "unverified" {
		t.Fatalf("layer = %+v, want the uid fallback once the map is stale", got)
	}
	if _, ok := namespaceMapLookup(4026532001); ok {
		t.Fatal("a stale map must read as empty")
	}
}

func TestNamespaceMapIsDisabledWithoutAPath(t *testing.T) {
	withProcRoot(t, t.TempDir())
	old := namespaceMapPath
	namespaceMapPath = ""
	resetNamespaceMap()
	t.Cleanup(func() { namespaceMapPath = old })
	if _, ok := namespaceMapLookup(4026532001); ok {
		t.Fatal("lookup must fail when no map is configured")
	}
	got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS())
	if got.Layer != "unverified" || got.Source != "namespace+uid" {
		t.Fatalf("layer = %+v, want the uid fallback with no map configured", got)
	}
}
