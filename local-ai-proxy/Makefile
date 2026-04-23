GO ?= go
PKG := ./...

# Release build flags:
#   -trimpath        strip local filesystem paths from the binary
#   -buildvcs=false  don't embed VCS info (reproducible across dirty/clean checkouts)
BUILDFLAGS := -trimpath -buildvcs=false

.PHONY: build test test-race test-synctest vet fmt tidy run clean verify vulncheck check

build: verify
	$(GO) build $(BUILDFLAGS) -o bin/local-ai-proxy .

test:
	$(GO) test $(PKG)

test-race:
	$(GO) test -race $(PKG)

# System tests: spin up real HTTP backends, launch the proxy binary as
# a subprocess, drive traffic over real TCP. Gated by the `system`
# build tag so they don't run on the default `test` target. Requires
# `make build` first — the tests invoke bin/local-ai-proxy.
test-system: build
	$(GO) test -tags system -count=1 $(PKG)

# testing/synctest is experimental in Go 1.24; drop this target on 1.25+.
test-synctest:
	GOEXPERIMENT=synctest $(GO) test -race $(PKG)

vet:
	$(GO) vet $(PKG)

fmt:
	$(GO) fmt $(PKG)

tidy:
	$(GO) mod tidy

# Cryptographically verify that module cache contents match go.sum.
# Fails the build if any dependency has been tampered with locally.
verify:
	$(GO) mod verify

# Scan pinned deps for known CVEs. Requires govulncheck:
#   go install golang.org/x/vuln/cmd/govulncheck@latest
# Skipped with a notice if not installed, so `make check` stays runnable
# without extra tooling.
vulncheck:
	@if command -v govulncheck >/dev/null 2>&1; then \
		govulncheck $(PKG); \
	else \
		echo "govulncheck not installed; skipping (go install golang.org/x/vuln/cmd/govulncheck@latest)"; \
	fi

check: verify vet test vulncheck

run: build
	./bin/local-ai-proxy

clean:
	rm -rf bin/ coverage.*
