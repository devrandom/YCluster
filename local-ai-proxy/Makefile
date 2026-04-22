GO ?= go
PKG := ./...

.PHONY: build test test-race vet fmt tidy run clean

build:
	$(GO) build -o bin/local-ai-proxy .

test:
	$(GO) test $(PKG)

test-race:
	$(GO) test -race $(PKG)

# testing/synctest is experimental in Go 1.24; drop this target on 1.25+.
test-synctest:
	GOEXPERIMENT=synctest $(GO) test -race $(PKG)

vet:
	$(GO) vet $(PKG)

fmt:
	$(GO) fmt $(PKG)

tidy:
	$(GO) mod tidy

run: build
	./bin/local-ai-proxy

clean:
	rm -rf bin/ coverage.*
