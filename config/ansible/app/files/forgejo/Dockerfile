ARG FORGEJO_BASE_IMAGE
FROM ${FORGEJO_BASE_IMAGE}

USER root
# Alpine v3.24 security bumps for packages the upstream image still ships
# vulnerable: Brotli 1.2.0, expat 2.8.5, zlib 1.3.2.
RUN apk add --no-cache --upgrade \
    'brotli-libs=1.2.1-r0' \
    'expat=2.9.0-r0' \
    'zlib=1.3.2-r1'
