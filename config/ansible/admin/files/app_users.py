"""Authorization and action helpers for the admin user roster."""


def authorized_projects(identity, admin, authentik_manager):
    """Projects a manager may touch; admins bypass."""
    if admin:
        return None
    values = authentik_manager.manager_projects(identity.email) if identity.email else []
    return {p.strip() for p in values if p and p.strip()}


def admin_user_action(action, email, authentik_manager):
    """Run one role-changing action that is restricted to admins."""
    if action == 'uninvite':
        count = authentik_manager.revoke_invitation(email)
        return {'ok': True, 'message': f'revoked {count} invitation(s)'}, None
    remove = action == 'admin_remove' or action == 'manager_remove'
    if action == 'admin_add' or action == 'admin_remove':
        return {'ok': True, 'message': authentik_manager.set_admin(email, remove=remove)}, None
    if action == 'manager_add' or action == 'manager_remove':
        return {'ok': True, 'message': authentik_manager.set_manager(email, remove=remove)}, None
    if action == 'activate' or action == 'deactivate':
        return {'ok': True, 'message': authentik_manager.set_active(email, active=(action == 'activate'))}, None
    return {'error': 'unknown action'}, 400


def invite_user(email, data, projects, authentik_manager, admin=False):
    """Invite a user, enforcing manager project scope when not an admin."""
    project = (data.get('project_name') or '').strip()
    if not admin:
        # Managers must invite into one of their authorized projects.
        # An empty selection is a missing required field (the invite
        # form's default), not an authorization failure.
        if not project:
            return {'error': 'project required'}, 400
        if project not in projects:
            return {'error': 'not authorized for project'}, 403
    days = max(1, min(90, int(data.get('days') or 7)))
    url = authentik_manager.invite_user(
        email, data.get('name') or None, days, project=project or None)
    return {'ok': True, 'url': url}, None


def recovery_user(email, projects, authentik_manager, admin=False):
    """Return a recovery URL after checking manager project access."""
    if not admin and (authentik_manager.user_project(email) or '') not in projects:
        return {'error': 'not authorized for this user'}, 403
    return {'ok': True, 'url': authentik_manager.recovery_link(email)}, None


def set_active_user(email, active, projects, authentik_manager, admin=False):
    """Activate or deactivate an account after manager project check."""
    if not admin and (authentik_manager.user_project(email) or '') not in projects:
        return {'error': 'not authorized for this user'}, 403
    return admin_user_action(
        'activate' if active else 'deactivate', email, authentik_manager)


def edit_user(email, data, projects, authentik_manager, admin=False):
    """Edit an account, limiting managers to the Notes field."""
    if not admin and (authentik_manager.user_project(email) or '') not in projects:
        return {'error': 'not authorized for this user'}, 403
    if admin:
        # update_user skips absent fields; passing '' clears.
        message = authentik_manager.update_user(
            email,
            name=(data.get('name') or '').strip() or None,
            project_name=data.get('project_name'),
            admin_notes=data.get('admin_notes'))
    else:
        # Managers may only touch the Notes field.
        message = authentik_manager.update_user(
            email, admin_notes=data.get('admin_notes'))
    return {'ok': True, 'message': message}, None
