"""Admin project/quota routes.

Extracted from app.py as a registration function rather than a Blueprint so
the handlers keep using the app's identity helper and logger without a
circular import.
"""

import secrets

from flask import jsonify, render_template, request

import app_project_db
import app_projects


def register(app, authentik_identity):
    """Attach the project/quota routes to the admin Flask app."""
    _authentik_identity = authentik_identity

    @app.route('/admin/projects')
    def projects_page():
        """Quota projects page. Admins get full control; managers get
        read-only visibility scoped to their authorized IdP projects."""
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        return render_template('admin-projects.html')

    def _project_transaction(operation, identity, *args):
        """Run a project-store operation in one audited database transaction."""
        conn = app_project_db.connect()
        request_id = secrets.token_urlsafe(12)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('SET LOCAL ycluster.request_id = %s',
                                (request_id,))
                    app_projects.set_actor(cur, identity)
                    result = operation(cur, identity, *args)
            return result, None
        except app_projects.ValidationError as e:
            return None, (jsonify({'error': str(e)}), 400)
        except Exception:
            app.logger.exception('project transaction failed')
            return None, (jsonify({'error': 'database unavailable'}), 503)
        finally:
            conn.close()

    def _project_response(result, error):
        if error:
            return error
        return jsonify(result)

    def _manager_idp_projects(id):
        """IdP projects a manager is authorized for; admins get None (all)."""
        from ycluster.utils import authentik_manager
        if id.is_admin:
            return None
        return authentik_manager.manager_projects(id.email) if id.email else []

    def _scoped_users(users, allowed_idp):
        """Filter the Authentik roster to the manager's IdP projects.

        A manager must never see user data outside their IdP-project scope.
        Admins (allowed_idp is None) see everyone.
        """
        if allowed_idp is None:
            return users
        allowed = set(allowed_idp)
        return [u for u in users
                if (u.get('project_name') or '') in allowed]

    @app.route('/admin/projects/data')
    def projects_data():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        from ycluster.utils import authentik_manager
        allowed_idp = _manager_idp_projects(id)
        conn = app_project_db.connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    if allowed_idp is None:
                        projects = app_projects.list_projects(cur)
                    else:
                        projects = app_projects.list_projects_for_idp(
                            cur, set(allowed_idp))
                    for project in projects:
                        project_id = project['id']
                        project['members'] = app_projects.list_members(cur,
                                                                      project_id)
                        project['quotas'] = app_projects.list_quotas(cur,
                                                                     project_id)
                    users = _scoped_users(authentik_manager.users_data(),
                                          allowed_idp)
        except Exception:
            app.logger.exception('projects_data failed')
            return jsonify({'error': 'database unavailable'}), 503
        finally:
            conn.close()
        if allowed_idp is None:
            known_idp_projects = sorted(
                {u['project_name'] for u in users if u.get('project_name')}
                | {p for u in users for p in (u.get('manager_projects') or [])
                   if p})
        else:
            # Managers get their full authorized IdP-project list (it may span
            # multiple projects, including ones without user projects yet).
            # Their own authorization list is not sensitive data.
            known_idp_projects = sorted(set(allowed_idp))
        return jsonify({'projects': projects,
                        'is_admin': id.is_admin,
                        'is_manager': id.is_manager,
                        'can_manage': id.is_admin or id.is_manager,
                        'idp_projects': known_idp_projects,
                        'users': [{'uid': str(u['uid']),
                                   'username': u.get('username') or u['email'],
                                   'email': u['email'],
                                   'name': u['name']} for u in users]})

    @app.route('/admin/projects/create', methods=['POST'])
    def project_create():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        idp_project = data.get('idp_project') or ''
        if not id.is_admin:
            # Managers may only create projects under IdP projects they manage.
            allowed = set(_manager_idp_projects(id))
            if idp_project not in allowed:
                return jsonify(
                    {'error': 'choose one of your managed IdP projects'}), 403
        result, error = _project_transaction(
            app_projects.create_project, id, data.get('id'),
            data.get('name'), data.get('description') or '', idp_project)
        return _project_response(result, error)

    @app.route('/admin/projects/update', methods=['POST'])
    def project_update():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        result, error = _project_transaction(
            app_projects.update_project, id, data.get('id'),
            data.get('name'), data.get('description'),
            data.get('active'), data.get('idp_project'),
            _manager_idp_projects(id))
        return _project_response(result, error)

    @app.route('/admin/projects/member/add', methods=['POST'])
    def project_member_add():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        result, error = _project_transaction(
            app_projects.add_member, id, data.get('project_id'),
            data.get('uid'), data.get('email'),
            data.get('is_default') if data.get('is_default') is not None
            else False, _manager_idp_projects(id))
        return _project_response({'ok': True}, error)

    @app.route('/admin/projects/member/remove', methods=['POST'])
    def project_member_remove():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        result, error = _project_transaction(
            app_projects.remove_member, id, data.get('project_id'),
            data.get('uid'), _manager_idp_projects(id))
        return _project_response({'ok': True}, error)

    @app.route('/admin/projects/member/default', methods=['POST'])
    def project_member_default():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        result, error = _project_transaction(
            app_projects.set_default_member, id, data.get('project_id'),
            data.get('uid'), data.get('email'), _manager_idp_projects(id))
        return _project_response({'ok': True}, error)

    @app.route('/admin/projects/quotas/upsert', methods=['POST'])
    def project_quota_upsert():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        result, error = _project_transaction(
            app_projects.upsert_quota, id, data.get('project_id'),
            data.get('quota_type'), data.get('quota_id'), data.get('value'),
            _manager_idp_projects(id))
        return _project_response(result, error)

    @app.route('/admin/projects/quotas/delete', methods=['POST'])
    def project_quota_delete():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        data = request.get_json(silent=True) or {}
        result, error = _project_transaction(
            app_projects.delete_quota, id, data.get('project_id'),
            data.get('quota_type'), data.get('quota_id'),
            _manager_idp_projects(id))
        return _project_response({'ok': True}, error)

    @app.route('/admin/projects/import', methods=['POST'])
    def project_quotas_import():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        # Admins see all projects (None); managers are restricted to the IdP
        # projects they manage (import_quotas rejects out-of-scope rows).
        allowed_idp = _manager_idp_projects(id)
        upload = request.files.get('file')
        if upload:
            csv_data = upload.read()
        else:
            csv_data = (request.get_json(silent=True) or {}).get('csv') or ''
        result, error = _project_transaction(
            app_projects.import_quotas, id, csv_data, allowed_idp)
        return _project_response(result, error)

    @app.route('/admin/projects/audit')
    def project_audit():
        id = _authentik_identity()
        if not id.is_manager:
            return jsonify({'error': 'admin only'}), 403
        limit = request.args.get('limit', 200, type=int)
        allowed_idp = _manager_idp_projects(id)
        conn = app_project_db.connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    if allowed_idp is None:
                        events = app_projects.audit_log(
                            cur, request.args.get('project_id'), limit)
                    else:
                        visible = {p['id'] for p in app_projects
                                   .list_projects_for_idp(cur, set(allowed_idp))}
                        events = [e for e in app_projects.audit_log(
                            cur, request.args.get('project_id'), limit)
                            if e.get('entity_id', '').split('/')[0] in visible
                            or e.get('entity_id', '') in visible]
        except app_projects.ValidationError as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            app.logger.exception('project_audit failed')
            return jsonify({'error': 'database unavailable'}), 503
        finally:
            conn.close()
        return jsonify({'events': events})
