package main

import (
	"fmt"
	"net"
	"net/http"
)

// TrustedHeadersMiddleware strips identity headers (X-User-Id) from any
// request whose remote address is not in the configured trusted-proxy
// set. That way a reverse proxy in front of us can inject X-User-Id
// after authenticating a client, while direct hits from anywhere else
// can't forge it.
//
// cidrs may be empty/nil; in that case loopback is trusted.
func TrustedHeadersMiddleware(cidrs []string, next http.Handler) (http.Handler, error) {
	nets, err := parseCIDRs(cidrs)
	if err != nil {
		return nil, err
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !remoteIsTrusted(r, nets) {
			r.Header.Del("X-User-Id")
		}
		next.ServeHTTP(w, r)
	}), nil
}

func parseCIDRs(in []string) ([]*net.IPNet, error) {
	if len(in) == 0 {
		in = DefaultTrustedProxies
	}
	out := make([]*net.IPNet, 0, len(in))
	for _, s := range in {
		_, n, err := net.ParseCIDR(s)
		if err != nil {
			return nil, fmt.Errorf("trusted_proxies: %q: %w", s, err)
		}
		out = append(out, n)
	}
	return out, nil
}

func remoteIsTrusted(r *http.Request, nets []*net.IPNet) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	for _, n := range nets {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}
