package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// publishNamespaceMap writes the file the root-side enumerator produces and
// points the forwarder at it.
func publishNamespaceMap(t *testing.T, entries map[string]string) {
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
	publishNamespaceMap(t, map[string]string{
		"4026532001": "0::/lxc.payload.tenant/init.scope\n",
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
		publishNamespaceMap(t, map[string]string{"4026532001": tc.cgroup})
		got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS())
		if got.Layer != "unverified" {
			t.Fatalf("%s: layer = %+v, want unverified", tc.name, got)
		}
	}
}

func TestNamespaceMapIgnoresAnUnknownNamespace(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string]string{"4026532999": "0::/lxc.payload.other/init.scope\n"})
	if got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS()); got.Layer != "unverified" {
		t.Fatalf("layer = %+v, want unverified for a namespace that is not published", got)
	}
}

func TestNamespaceMapExpiresWhenThePublisherStops(t *testing.T) {
	withProcRoot(t, t.TempDir())
	publishNamespaceMap(t, map[string]string{"4026532001": "0::/lxc.payload.tenant/init.scope\n"})
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
	if got := resolveProcessLayer(4242, "2026-01-01T00:00:00Z", 1000, containerNS()); got.Source == "nsmap" {
		t.Fatalf("layer = %+v, want no nsmap source", got)
	}
}
