# Disabled rule layer content

These SigmaHQ rules match `DestinationHostname`, but the Tetragon normalizer
has no DNS or hostname enrichment, so they can never fire. They are kept for
provenance and are not installed under `/etc/rsigma/rules`.

- `net_connection_lnx_crypto_mining_indicators.yml`
- `net_connection_lnx_ngrok_tunnel.yml`
- `net_connection_lnx_domain_localtonet_tunnel.yml`

Re-enable them only after the pipeline resolves destination addresses to
hostnames.
