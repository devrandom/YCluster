package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"strings"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// newEtcdClient builds a clientv3.Client from the given EtcdConfig, layering
// the ycluster cluster-wide etcd environment on top. That environment lives in
// /etc/ycluster/etcd-client.env and is injected via systemd
// EnvironmentFile=/etc/ycluster/etcd-client.env, exactly like every other
// cluster etcd client (Python via common/etcd_utils.py, shell via `source`).
// Reading it here keeps local-ai-proxy in lockstep with the etcd_tls_phase
// rollout (off → listen → connect → enforce) without a separate config path.
//
// Endpoints come from ETCD_HOSTS (comma-separated host:port), falling back to
// the YAML etcd.endpoints, then DefaultEtcdEndpoint. TLS comes from
// ETCD_CACERT / ETCD_CERT / ETCD_KEY; any of them present enables mTLS, none
// means a plaintext connection (the pre-mTLS / dev default).
func newEtcdClient(cfg *EtcdConfig) (*clientv3.Client, error) {
	tlsConfig, err := etcdTLSConfig()
	if err != nil {
		return nil, err
	}
	return clientv3.New(clientv3.Config{
		Endpoints:   etcdEndpoints(cfg),
		DialTimeout: 5 * time.Second,
		Username:    cfg.Username,
		Password:    cfg.Password,
		TLS:         tlsConfig,
	})
}

// etcdEndpoints resolves the endpoint list from ETCD_HOSTS, then the YAML
// config, then the package default. Entries are host:port (no scheme); when
// TLS is configured clientv3 dials them over TLS regardless.
func etcdEndpoints(cfg *EtcdConfig) []string {
	if env := strings.TrimSpace(os.Getenv("ETCD_HOSTS")); env != "" {
		var eps []string
		for _, h := range strings.Split(env, ",") {
			if h = strings.TrimSpace(h); h != "" {
				eps = append(eps, h)
			}
		}
		if len(eps) > 0 {
			return eps
		}
	}
	if len(cfg.Endpoints) > 0 {
		return cfg.Endpoints
	}
	return []string{DefaultEtcdEndpoint}
}

// etcdTLSConfig returns a *tls.Config when client-cert material is present in
// the environment, or nil for a plaintext connection.
func etcdTLSConfig() (*tls.Config, error) {
	caFile := os.Getenv("ETCD_CACERT")
	certFile := os.Getenv("ETCD_CERT")
	keyFile := os.Getenv("ETCD_KEY")
	if caFile == "" && certFile == "" && keyFile == "" {
		return nil, nil
	}

	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if caFile != "" {
		caPEM, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("read etcd CA %s: %w", caFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("etcd CA %s: no certificates parsed", caFile)
		}
		tlsConfig.RootCAs = pool
	}
	if certFile != "" || keyFile != "" {
		if certFile == "" || keyFile == "" {
			return nil, fmt.Errorf("etcd client cert and key must both be set (cert=%q key=%q)", certFile, keyFile)
		}
		cert, err := tls.LoadX509KeyPair(certFile, keyFile)
		if err != nil {
			return nil, fmt.Errorf("load etcd client keypair: %w", err)
		}
		tlsConfig.Certificates = []tls.Certificate{cert}
	}
	return tlsConfig, nil
}
