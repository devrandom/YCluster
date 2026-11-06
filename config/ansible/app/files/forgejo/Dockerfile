ARG FORGEJO_BASE_IMAGE
FROM ${FORGEJO_BASE_IMAGE}

USER root
# The 17.0.1 base already ships curl 8.23.0-r0, brotli-libs 1.2.1-r0, and
# zlib 1.3.2-r1. Two packages are still behind: OpenSSH 10.3_p1-r1
# (ALPINE-CVE-2026-123158, fixed in 10.3_p1-r2) and libexpat 2.9.0-r0
# (fixed in 2.10.0-r0).
RUN apk add --no-cache --upgrade \
    'libexpat=2.10.0-r0' \
    'openssh=10.3_p1-r2'
