package main

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"net/netip"
	"os"
	"strconv"
	"time"
)

// destinationKey is the per-host key behind the dest label. The alert text
// redacts addresses, but Alertmanager fingerprints an alert from its labels
// and ntfy-webhook dedups on that fingerprint, so two destinations would
// otherwise collapse after leaving the forwarder. The label carries a keyed
// hash instead of the address: distinct on the wire, not enumerable off it.
var destinationKey = makeDestinationKey()

func makeDestinationKey() []byte {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		sum := sha256.Sum256([]byte(strconv.FormatInt(time.Now().UnixNano(), 10)))
		return sum[:]
	}
	return key
}

// loadDestinationKey reads the key file, creating it on first use. An
// ephemeral key is still usable - destinations just stop being comparable
// across a restart, which costs a duplicate notification at most.
func loadDestinationKey(path string, logf func(string, ...any)) []byte {
	if path != "" {
		if body, err := os.ReadFile(path); err == nil && len(body) >= 16 {
			return body
		}
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		logf("destination key: %v (destinations will not be comparable across restarts)", err)
	}
	if path != "" {
		if err := os.WriteFile(path, key, 0o600); err != nil {
			logf("write destination key %s: %v", path, err)
		}
	}
	return key
}

// destinationLabel is the label that separates one TCP destination from
// another without publishing the address.
func destinationLabel(addr netip.Addr, port uint16) string {
	if !addr.IsValid() {
		return ""
	}
	suffix := strconv.Itoa(int(port))
	mac := hmac.New(sha256.New, destinationKey)
	mac.Write([]byte(addr.String()))
	return hex.EncodeToString(mac.Sum(nil)[:8]) + "/" + suffix
}
