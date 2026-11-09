# Karma image built in-cluster from the pinned source checkout.
#
# Mirrors upstream's Dockerfile, except for the final stage: upstream uses
# gcr.io/distroless/static, which the build host cannot reach. Karma is
# CGO-free and only talks plain HTTP to the cluster Alertmanagers, so a
# scratch stage is sufficient. Revisit if an upstream is ever added over TLS
# (that would need a CA bundle).
FROM node:25.9.0-alpine AS nodejs-builder
RUN mkdir -p /src/ui
COPY ui/package.json ui/package-lock.json /src/ui/
RUN cd /src/ui && npm ci && touch node_modules/.install
RUN apk add make git
COPY ui /src/ui
RUN make -C /src/ui build

FROM golang:1.27.3-alpine AS go-builder
RUN apk add make git
COPY Makefile /src/Makefile
COPY make /src/make
COPY go.mod /src/go.mod
COPY go.sum /src/go.sum
RUN make -C /src download-deps-go
COPY --from=nodejs-builder /src/ui/src /src/ui/src
COPY --from=nodejs-builder /src/ui/dist /src/ui/dist
COPY --from=nodejs-builder /src/ui/mock /src/ui/mock
COPY --from=nodejs-builder /src/ui/embed.go /src/ui/embed.go
COPY cmd /src/cmd
COPY internal /src/internal
ARG VERSION
RUN CGO_ENABLED=0 make -C /src VERSION="${VERSION:-dev}" karma

FROM scratch
ARG VERSION
LABEL org.opencontainers.image.source=https://github.com/prymitive/karma
LABEL org.opencontainers.image.version=${VERSION}
COPY --from=go-builder /src/karma /karma
EXPOSE 8080
ENTRYPOINT ["/karma"]
