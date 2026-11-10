"""Self-service settings and usage routes for the admin app.

Registered from app.py with the app module, so handlers reuse its identity
helper, vm_manager and usage helpers without a circular import.
"""

import re
from datetime import datetime, timezone

from flask import Response, jsonify, render_template, request

VM_USAGE_SINCE_VALUES = {'1', '7', '14', '30', '60', '90',
                         'this-year', 'previous-year'}

INFERENCE_API_PATH = '/v1'
_NON_API_HOST_PREFIXES = ('admin', 'app')
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


def _external_inference_endpoint(mod):
    """Public inference base URL (`https://<domain>/v1`) for the keys page.

    The inference API is served at the root `<domain>` vhost, not on the
    admin/app vhost this page runs on. Prefer the canonical domain from
    etcd; when it is unset (dev/force-local install) derive `<domain>` from
    the request host, dropping an admin/app subdomain. Returns '' when no
    usable domain can be determined."""
    try:
        domain = mod._configured_external_domain(mod.get_etcd_client())
    except Exception:
        domain = ''
    if not domain:
        host = (request.host or '').split(':', 1)[0]
        head, sep, rest = host.partition('.')
        if sep and head in _NON_API_HOST_PREFIXES:
            host = rest
        if ('.' in host and not host.endswith('.xc')
                and not _IPV4_RE.match(host)):
            domain = host
    return f'https://{domain}{INFERENCE_API_PATH}' if domain else ''


def _vm_usage_since():
    """Raw ?since value for the VM usage page, validated against the UI."""
    raw = request.args.get('since', '7')
    return raw if raw in VM_USAGE_SINCE_VALUES else '7'


def _vm_usage_window(since_days_dt):
    """UTC (start, end) for the VM usage query: ?since=N days (max 90),
    'this-year' (Jan 1 -> now), or 'previous-year' (capped at Dec 31)."""
    raw = _vm_usage_since()
    now = datetime.now(timezone.utc)
    if raw in ('this-year', 'previous-year'):
        year = now.year if raw == 'this-year' else now.year - 1
        return (datetime(year, 1, 1, tzinfo=timezone.utc),
                min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), now))
    return since_days_dt(int(raw)), now


def register(mod):
    """Attach the settings and usage routes to ``mod.app``."""
    flask_app = mod.app
    @flask_app.route('/admin/settings')
    def settings_page():
        """Self-service settings — the logged-in user's own SSH keys for VM
        access. Served on the app.<domain> vhost (see ycluster_app.conf.j2)."""
        return render_template('admin-settings.html')


    @flask_app.route('/admin/settings/data')
    def settings_data():
        id = mod._authentik_identity()
        if not id.email:
            # No forward-auth identity (internal / direct :12723) → there is no
            # "logged-in user" to self-manage; operators use `ycluster vm ssh`.
            return jsonify({'error': 'no authenticated user'}), 403
        try:
            rec = mod.vmm.user_get(id.email) or {}
        except Exception as e:
            return jsonify({'error': f'etcd connection failed: {e}'}), 503
        return jsonify({'email': id.email,
                        'is_admin': id.is_admin,
                        'is_manager': id.is_manager,
                        'keys': rec.get('ssh_keys', [])})


    def _settings_key_payload():
        """Validate caller identity and SSH key.

        Returns (identity, key, error_response). The email comes from the auth
        header only, so a caller can edit only its own /cluster/users record.
        Bastion and VM key syncs are handled by watchers, never inline here.
        """
        id = mod._authentik_identity()
        if not id.email:
            return id, None, (jsonify({'error': 'no authenticated user'}), 403)
        key = (request.get_json(silent=True) or {}).get('key', '')
        if not isinstance(key, str) or not key.strip():
            return id, None, (jsonify({'error': 'no key provided'}), 400)
        return id, key, None


    @flask_app.route('/admin/settings/keys/add', methods=['POST'])
    def settings_keys_add():
        id, key, error = _settings_key_payload()
        if error:
            return error
        try:
            mod.vmm.user_add_key(id.email, key)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400        # bad key format
        except Exception as e:
            return jsonify({'error': f'etcd write failed: {e}'}), 503
        return _settings_keys_response(id.email)


    @flask_app.route('/admin/settings/keys/remove', methods=['POST'])
    def settings_keys_remove():
        id, key, error = _settings_key_payload()
        if error:
            return error
        try:
            rec = mod.vmm.user_get(id.email) or {}
            if key not in rec.get('ssh_keys', []):
                raise KeyError('key not found')
            # Full key line: distinct keys are never substrings of each other.
            mod.vmm.user_remove_key(id.email, key)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': f'etcd write failed: {e}'}), 503
        return _settings_keys_response(id.email)


    def _settings_keys_response(email):
        """Fresh key list for `email`, re-read after a mutation."""
        rec = mod.vmm.user_get(email) or {}
        return jsonify({'keys': rec.get('ssh_keys', [])})


    @flask_app.route('/admin/settings/api-keys')
    def settings_api_keys_page():
        """Self-service page for the logged-in user's inference gateway API
        keys. Mint-once / present-once — the plaintext is only returned in the
        create response and never re-displayed."""
        return render_template('admin-api-keys.html')


    @flask_app.route('/admin/settings/api-keys/data')
    def settings_api_keys_data():
        id = mod._authentik_identity()
        if not id.email:
            return jsonify({'error': 'no authenticated user'}), 403
        try:
            data = mod.apikeys.list_keys(id.email)
        except Exception as e:
            return jsonify({'error': f'etcd connection failed: {e}'}), 503
        return jsonify({'email': id.email,
                        'endpoint': _external_inference_endpoint(mod),
                        'keys': data.get(id.email, [])})


    @flask_app.route('/admin/settings/api-keys/create', methods=['POST'])
    def settings_api_keys_create():
        id = mod._authentik_identity()
        if not id.email:
            return jsonify({'error': 'no authenticated user'}), 403
        body = request.get_json(silent=True) or {}
        label = body.get('label', '') or ''
        if not isinstance(label, str):
            return jsonify({'error': 'invalid label'}), 400
        try:
            secret = mod.apikeys.create_key(id.email, label=label)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': f'etcd write failed: {e}'}), 503
        # The plaintext is returned exactly once; it is not stored and there is
        # no re-display endpoint.
        return jsonify({'secret': secret, 'label': label})


    @flask_app.route('/admin/settings/api-keys/revoke', methods=['POST'])
    def settings_api_keys_revoke():
        id = mod._authentik_identity()
        if not id.email:
            return jsonify({'error': 'no authenticated user'}), 403
        body = request.get_json(silent=True) or {}
        key_id = body.get('key_id', '') or ''
        if not isinstance(key_id, str) or not key_id.strip():
            return jsonify({'error': 'no key id provided'}), 400
        try:
            # Scoped: only revoke if the key is owned by the caller.
            mine = {r['key_id'] for r in mod.apikeys.list_keys(id.email).get(id.email, [])}
            if key_id not in mine:
                return jsonify({'error': 'key not found'}), 404
            mod.apikeys.revoke_key(key_id)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': f'etcd write failed: {e}'}), 503
        return jsonify({'ok': True})


    @flask_app.route('/admin/settings/api-keys/rename', methods=['POST'])
    def settings_api_keys_rename():
        id = mod._authentik_identity()
        if not id.email:
            return jsonify({'error': 'no authenticated user'}), 403
        body = request.get_json(silent=True) or {}
        key_id = body.get('key_id', '') or ''
        label = body.get('label', '') or ''
        if not isinstance(key_id, str) or not key_id.strip():
            return jsonify({'error': 'no key id provided'}), 400
        if not isinstance(label, str):
            return jsonify({'error': 'invalid label'}), 400
        label = label.strip()
        try:
            # Owner-scoped in the DAL: another user's key-id is not found.
            if not mod.apikeys.rename_key(id.email, key_id, label):
                return jsonify({'error': 'key not found'}), 404
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': f'etcd write failed: {e}'}), 503
        return jsonify({'ok': True, 'key_id': key_id, 'label': label})


    @flask_app.route('/admin/vm-usage')
    def vm_usage_page():
        """VM GPU-hour accounting: billable/tracked (events) vs observed (samples)."""
        return render_template('admin-vm-usage.html', since=_vm_usage_since())


    def _vm_usage_rows():
        """Per-VM GPU-hour rows for the selected window, or (None, error).

        Shared by the JSON endpoint and the CSV export so both always
        describe the same rows.
        """
        from app_usage import (fetch_usage_state, gpu_types_by_host,
                               owner_visibility, registry_snapshots,
                               usage_rows)
        since_dt, until_dt = _vm_usage_window(mod._since_days_dt)
        conn, _, err = mod._usage_stats_cursor(since_dt)
        if err:
            return None, err
        assert conn is not None

        with conn:
            with conn.cursor() as cur:
                events, sampled, last_state, host_down = fetch_usage_state(
                    cur, since_dt, until_dt)

        registry, desired_all = registry_snapshots(mod.vmm)
        # Samples that show a VM stopped bound its still-open interval:
        # a missed stop event must not bill or track forever.
        observed_end = {vm: s['stopped_at'] for vm, s in sampled.items()
                        if s.get('stopped_at')}
        per_vm = mod._assemble_intervals(events, since_dt, until_dt,
                                         observed_end=observed_end,
                                         host_down=host_down)
        from ycluster.utils import authentik_manager
        visible = owner_visibility(mod._authentik_identity(), authentik_manager)
        rows = usage_rows(per_vm, sampled, registry, desired_all, last_state,
                          visible, mod._usage_row,
                          gpu_types=gpu_types_by_host(registry))
        return rows, None


    @flask_app.route('/admin/vm-usage/data')
    def vm_usage_data():
        """Per-VM GPU-hours in the window: billable + tracked from lifecycle
        events (interval assembly), observed from incus-state samples."""
        rows, err = _vm_usage_rows()
        if err:
            return jsonify({'error': err}), 500
        return jsonify(rows)


    @flask_app.route('/admin/vm-usage/export.csv')
    def vm_usage_export():
        """Download the current VM-usage view as CSV."""
        from app_usage import usage_csv
        rows, err = _vm_usage_rows()
        if err:
            return jsonify({'error': err}), 500
        filename = f'vm-usage-{_vm_usage_since()}.csv'
        return Response(usage_csv(rows).getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition':
                                 f'attachment; filename={filename}'})


    @flask_app.route('/admin/model-usage/options')
    def model_usage_options():
        """JSON endpoint returning distinct users and models for filter dropdowns.
        Admin-only, like the rest of the model-usage surface."""
        if not mod._authentik_identity().is_admin:
            return jsonify({'error': 'admin only'}), 403
        since_days = max(1, min(90, request.args.get('since', 7, type=int)))

        conn, since_dt, err = mod._usage_stats_cursor(mod._since_days_dt(since_days))
        if err:
            return jsonify({'error': err}), 500
        assert conn is not None

        with conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT DISTINCT user_id FROM model_usage WHERE period_start >= %s ORDER BY user_id''', [since_dt])
                users = [str(r[0]) for r in cur.fetchall()]
                cur.execute('''SELECT DISTINCT model FROM model_usage WHERE period_start >= %s ORDER BY model''', [since_dt])
                models = [str(r[0]) for r in cur.fetchall()]
        return jsonify({'users': users, 'models': models})
