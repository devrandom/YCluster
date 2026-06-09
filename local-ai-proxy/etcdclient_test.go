package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEtcdEndpoints(t *testing.T) {
	tests := []struct {
		name     string
		envHosts string
		setEnv   bool
		cfg      EtcdConfig
		want     []string
	}{
		{
			name: "default when nothing set",
			cfg:  EtcdConfig{},
			want: []string{DefaultEtcdEndpoint},
		},
		{
			name: "yaml endpoints when no env",
			cfg:  EtcdConfig{Endpoints: []string{"http://a:2379", "http://b:2379"}},
			want: []string{"http://a:2379", "http://b:2379"},
		},
		{
			name:     "env wins over yaml",
			envHosts: "10.0.0.11:2381,10.0.0.12:2381",
			setEnv:   true,
			cfg:      EtcdConfig{Endpoints: []string{"http://a:2379"}},
			want:     []string{"10.0.0.11:2381", "10.0.0.12:2381"},
		},
		{
			name:     "env trims spaces and skips empties",
			envHosts: " 10.0.0.11:2381 , ,10.0.0.13:2381 ",
			setEnv:   true,
			cfg:      EtcdConfig{},
			want:     []string{"10.0.0.11:2381", "10.0.0.13:2381"},
		},
		{
			name:     "empty env falls back to yaml",
			envHosts: "   ",
			setEnv:   true,
			cfg:      EtcdConfig{Endpoints: []string{"http://a:2379"}},
			want:     []string{"http://a:2379"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if tt.setEnv {
				t.Setenv("ETCD_HOSTS", tt.envHosts)
			} else {
				os.Unsetenv("ETCD_HOSTS")
			}
			got := etcdEndpoints(&tt.cfg)
			if len(got) != len(tt.want) {
				t.Fatalf("got %v, want %v", got, tt.want)
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Fatalf("got %v, want %v", got, tt.want)
				}
			}
		})
	}
}

func TestEtcdTLSConfig(t *testing.T) {
	// No material anywhere → plaintext (nil config).
	t.Run("nil when unset", func(t *testing.T) {
		os.Unsetenv("ETCD_CACERT")
		os.Unsetenv("ETCD_CERT")
		os.Unsetenv("ETCD_KEY")
		tc, err := etcdTLSConfig()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if tc != nil {
			t.Fatalf("expected nil tls config, got %v", tc)
		}
	})

	// Cert without key is a misconfiguration, not a silent plaintext fallback.
	t.Run("cert without key errors", func(t *testing.T) {
		os.Unsetenv("ETCD_CACERT")
		t.Setenv("ETCD_CERT", "/tmp/does-not-matter.crt")
		os.Unsetenv("ETCD_KEY")
		if _, err := etcdTLSConfig(); err == nil {
			t.Fatal("expected error for cert without key")
		}
	})

	// A readable CA enables server-cert verification (RootCAs populated).
	t.Run("ca only sets RootCAs", func(t *testing.T) {
		caPath := filepath.Join(t.TempDir(), "ca.crt")
		if err := os.WriteFile(caPath, []byte(testCAPEM), 0o600); err != nil {
			t.Fatal(err)
		}
		os.Unsetenv("ETCD_CERT")
		os.Unsetenv("ETCD_KEY")
		t.Setenv("ETCD_CACERT", caPath)
		tc, err := etcdTLSConfig()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if tc == nil || tc.RootCAs == nil {
			t.Fatalf("expected RootCAs to be set, got %#v", tc)
		}
	})

	// A garbage CA file is a hard error, never a silent skip.
	t.Run("unparseable ca errors", func(t *testing.T) {
		caPath := filepath.Join(t.TempDir(), "ca.crt")
		if err := os.WriteFile(caPath, []byte("not a pem"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Setenv("ETCD_CACERT", caPath)
		if _, err := etcdTLSConfig(); err == nil {
			t.Fatal("expected error for unparseable CA")
		}
	})
}

// testCAPEM is a throwaway self-signed CA cert used only to verify that
// AppendCertsFromPEM succeeds; it is never used for a real connection.
const testCAPEM = `-----BEGIN CERTIFICATE-----
MIIBhTCCASugAwIBAgIQIRi6zePL6mKjOipn+dNuaTAKBggqhkjOPQQDAjASMRAw
DgYDVQQKEwdBY21lIENvMB4XDTE3MTAyMDE5NDMwNloXDTE4MTAyMDE5NDMwNlow
EjEQMA4GA1UEChMHQWNtZSBDbzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABD0d
7VNhbWvZLWPuj/RtHFjvtJBEwOkhbN/BnnE8rnZR8+sbwnc/KhCk3FhnpHZnQz7B
5aETbbIgmuvewdjvSBSjYzBhMA4GA1UdDwEB/wQEAwICpDATBgNVHSUEDDAKBggr
BgEFBQcDATAPBgNVHRMBAf8EBTADAQH/MCkGA1UdEQQiMCCCDmxvY2FsaG9zdDo1
NDUzgg4xMjcuMC4wLjE6NTQ1MzAKBggqhkjOPQQDAgNIADBFAiEA2zpJEPQyz6/l
Wf86aX6PepsntZv2GYlA5UpabfT2EZICICpJ5h/iI+i341gBmLiAFQOyTDT+/wQc
6MF9+Yw1Yy0t
-----END CERTIFICATE-----`
