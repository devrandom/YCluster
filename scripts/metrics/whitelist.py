"""Vulture whitelist: names referenced only through dynamic dispatch.

Vulture's whitelist mechanism imports this module; a bare reference marks the
name as used. Route handlers, callback hooks, plugin registrations, and
framework-consumed class attributes belong here — never genuinely dead code.
Each entry was verified against the framework contract it satisfies.
"""

# Flask route handlers (app.py) — dispatched by URL routing.
allocate_hostname
wg_register
wg_poll
get_dhcp_config
get_hosts
check_service_conditionally
drain_status
drain_status_target
inventory_get_hardware
inventory_get_asset
inventory_set_asset
inventory_list
inventory_export_csv
ping
get_time
serve_meta_data
serve_user_data
serve_bootstrap_index
serve_bootstrap
prometheus_metrics
alert_webhook
prometheus_targets
cluster_status_api
static_files
status_page
utilization_page
utilization_page_legacy
inventory_page
inventory_page_legacy
resources_page
resources_data
model_usage_page
model_usage_data
nav_identity
app_landing
vm_schedule_page
vm_schedule_data
vm_schedule_set
vm_schedule_stop_now
gpu_block_upsert
gpu_block_remove
users_page
users_action
settings_page
settings_data
settings_keys_add
settings_keys_remove
settings_api_keys_page
settings_api_keys_data
settings_api_keys_create
settings_api_keys_revoke
vm_usage_page
vm_usage_data
model_usage_options

# Flask before_request hook (health-api.py).
_init_request_cache

# Flask route (ntfy-webhook.py).
webhook

# HTTP-server overrides and config dataclasses.
do_GET
log_message
daemon_threads
request_queue_size

# FastAPI routes (whisperx server.py).
transcriptions
translations

# Dataclass/config schema fields read from YAML/etcd by other services.
nameservers
lifetime

# Django settings module contract (authentik-user-settings.py).
PASSWORD_HASHERS

# Ansible dynamic-inventory plugin contract (etcd_nodes.py).
DOCUMENTATION
REQUIREMENTS
NAME

# vLLM ReasoningParser plugin: class registered via
# @ReasoningParserManager.register_module; methods and parameters mirror the
# upstream interface signature.
MiniMaxM3ReasoningParser
ChatCompletionRequest
ResponsesRequest
previous_text
current_text
current_token_ids
MiniMaxM3ReasoningParser.extract_reasoning
MiniMaxM3ReasoningParser.is_reasoning_end_streaming
MiniMaxM3ReasoningParser.extract_content_ids
MiniMaxM3ReasoningParser.extract_reasoning_streaming

# Used in a string type annotation (dhcp_server.py cast(..., "list[...]")).
KVMetadata

# Flask routes (local-ai-proxy-auth.py and whisperx server.py).
auth
healthz


# Used only from the test suite (tests are excluded from the scan).
validate_key
reset_store

# Copied over the open-webui tree at build time; loaded by its retrieval
# plugin registry (build-open-webui.yml).
search_kagi

# Written as a psycopg2 connection attribute (apikeys.py).
autocommit
