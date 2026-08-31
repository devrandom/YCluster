"""Authorization helpers for the admin user roster."""


def authorized_projects(identity, admin, authentik_manager):
    """Projects a manager may touch; admins bypass."""
    if admin:
        return None
    values = authentik_manager.manager_projects(identity.email) if identity.email else []
    return {p.strip() for p in values if p and p.strip()}
