package main

import (
	"net/netip"
	"strconv"
	"strings"
)

// redactIPs replaces IPv4 and IPv6 addresses so no user IP reaches
// Alertmanager labels, annotations, or ntfy messages. Tokens are split on
// characters that cannot appear in an address, validated with
// netip.ParseAddr, and replaced individually.
func redactIPs(s string) string {
	if !strings.ContainsAny(s, ".:") {
		return s
	}
	var b strings.Builder
	for _, tok := range splitKeep(s, isSeparator) {
		b.WriteString(redactToken(tok))
	}
	return b.String()
}

func isSeparator(r rune) bool {
	switch r {
	case ' ', '\t', '\n', ',', ';', '"', '\'', '(', ')', '{', '}', '=', '<', '>', '|':
		return true
	}
	return false
}

func redactToken(tok string) string {
	if scheme, rest, ok := strings.Cut(tok, "://"); ok && isScheme(scheme) {
		return redactURL(scheme, rest, tok)
	}
	// user@host, user:pass@host, user@host:port/path and the scp form
	// user@host:/path all carry an address after the final "@".
	if at := strings.LastIndexByte(tok, '@'); at >= 0 {
		if redacted, ok := redactAddressSuffix(tok[at+1:]); ok {
			return tok[:at+1] + redacted
		}
	}
	if redacted, ok := redactBracketed(tok); ok {
		return redacted
	}
	trim := strings.Trim(tok, "[]")
	if trim == "" {
		return tok
	}
	if redacted, ok := redactCIDR(trim, tok); ok {
		return redacted
	}
	if redacted, ok := redactHostPort(trim, tok); ok {
		return redacted
	}
	if isAddr(trim) {
		return "[ip]"
	}
	return tok
}

// redactAddressSuffix redacts the address in "addr[:port][/path]" or
// "addr:path", keeping the port and path for triage.
func redactAddressSuffix(s string) (string, bool) {
	if s == "" {
		return "", false
	}
	if isAddr(strings.Trim(s, "[]")) {
		return "[ip]", true
	}
	host, trail := s, ""
	if strings.HasPrefix(s, "[") {
		end := strings.IndexByte(s, ']')
		if end < 1 {
			return "", false
		}
		host, trail = s[:end+1], s[end+1:]
	} else if i := strings.IndexAny(s, ":/"); i >= 0 {
		host, trail = s[:i], s[i:]
	}
	if !isAddr(strings.Trim(host, "[]")) {
		return "", false
	}
	return "[ip]" + trail, true
}

// redactURL redacts the host of a scheme://[userinfo@]host/... token.
func redactURL(scheme, rest, tok string) string {
	authority := rest
	if i := strings.IndexByte(authority, '/'); i >= 0 {
		authority = authority[:i]
	}
	userinfo := ""
	if at := strings.LastIndexByte(authority, '@'); at >= 0 {
		userinfo, rest = rest[:at+1], rest[at+1:]
	}
	if host, trail, ok := cutHost(rest); ok {
		if redacted := redactToken(host); redacted != host {
			return scheme + "://" + userinfo + redacted + trail
		}
	}
	return tok
}

// redactBracketed handles "[addr]...": bracketed IPv6 with an optional
// port or trailing path. Returns ok=false when the token is not bracketed
// or the bracket contents are not an address.
func redactBracketed(tok string) (string, bool) {
	if !strings.HasPrefix(tok, "[") {
		return "", false
	}
	closeIdx := strings.Index(tok, "]")
	if closeIdx < 1 {
		return "", false
	}
	inner := tok[1:closeIdx]
	if _, err := netip.ParseAddr(inner); err != nil {
		return "", false
	}
	return "[ip]" + tok[closeIdx+1:], true
}

// redactCIDR handles "addr/len" tokens, keeping the prefix length.
func redactCIDR(trim, tok string) (string, bool) {
	host, suffix, ok := strings.Cut(trim, "/")
	if !ok || suffix == "" {
		return "", false
	}
	if _, err := netip.ParseAddr(host); err != nil || !isPortOrLen(suffix) {
		return "", false
	}
	return "[ip]/" + suffix, true
}

// redactHostPort handles "ipv4:port" tokens, keeping the port.
func redactHostPort(trim, tok string) (string, bool) {
	host, port, ok := strings.Cut(trim, ":")
	if !ok || strings.Contains(host, ":") {
		return "", false
	}
	ip, err := netip.ParseAddr(host)
	if err != nil || !ip.Is4() || !isPort(port) {
		return "", false
	}
	return "[ip]:" + port, true
}

func isAddr(s string) bool {
	_, err := netip.ParseAddr(s)
	return err == nil
}

func isScheme(s string) bool {
	switch strings.ToLower(s) {
	case "http", "https", "ws", "wss":
		return true
	}
	return false
}

// cutHost splits "host[:port][/path]" into host and the remainder.
func cutHost(rest string) (string, string, bool) {
	if strings.HasPrefix(rest, "[") {
		closeIdx := strings.Index(rest, "]")
		if closeIdx < 0 {
			return "", "", false
		}
		return rest[:closeIdx+1], rest[closeIdx+1:], true
	}
	end := strings.IndexAny(rest, "/:")
	if end < 0 {
		return rest, "", true
	}
	return rest[:end], rest[end:], true
}

func isPort(s string) bool {
	if s == "" || len(s) > 5 {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// isPortOrLen reports whether s is a valid TCP/UDP port or IP prefix
// length (0-128, digits only, at most 5 chars). Used to decide whether a
// "/suffix" after an address is a prefix length worth preserving.
func isPortOrLen(s string) bool {
	if !isPort(s) {
		return false
	}
	n, err := strconv.Atoi(s)
	return err == nil && n >= 0 && n <= 128
}

func redactMap(m map[string]string) {
	for k, v := range m {
		m[k] = redactIPs(v)
	}
}

// splitKeep splits s on the rune class but keeps the separators as their
// own tokens so rejoining reconstructs the original string.
func splitKeep(s string, sep func(rune) bool) []string {
	var out []string
	start := 0
	for i, r := range s {
		if sep(r) {
			if i > start {
				out = append(out, s[start:i])
			}
			out = append(out, string(r))
			start = i + len(string(r))
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
