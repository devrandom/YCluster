"""
Cluster account management (authentik IdP)
"""


def register_user_commands(subparsers):
    """Register user management commands"""
    user_parser = subparsers.add_parser('user', help='Cluster account management (authentik)')
    user_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=user_parser)
    user_subparsers = user_parser.add_subparsers(dest='user_command', help='User commands')

    add_parser = user_subparsers.add_parser(
        'add', help='Create an account (no credentials — for external-login linking)')
    add_parser.add_argument('email', help='Account email (cluster identity key)')
    add_parser.add_argument('--name', help='Display name (defaults to email)')
    add_parser.set_defaults(func=user_add)

    invite_parser = user_subparsers.add_parser(
        'invite', help='Issue a single-use enrollment invitation URL')
    invite_parser.add_argument('email', help='Account email (cluster identity key)')
    invite_parser.add_argument('--name', help='Display name (defaults to email)')
    invite_parser.add_argument('--days', type=int, default=7, help='Expiry in days (default 7)')
    invite_parser.set_defaults(func=user_invite)

    admin_parser = user_subparsers.add_parser(
        'admin', help='Grant (or revoke) admin-pages access (ycluster-admins group)')
    admin_parser.add_argument('email', help='Account email')
    admin_parser.add_argument('--remove', action='store_true', help='Revoke instead of grant')
    admin_parser.set_defaults(func=user_admin)

    recovery_parser = user_subparsers.add_parser(
        'recovery', help='Issue a one-time password (re)set link for an existing account')
    recovery_parser.add_argument('email', help='Account email')
    recovery_parser.set_defaults(func=user_recovery)

    uninvite_parser = user_subparsers.add_parser(
        'uninvite', help='Revoke outstanding invitation(s) for an email')
    uninvite_parser.add_argument('email', help='Invited email')
    uninvite_parser.set_defaults(func=user_uninvite)

    import_parser = user_subparsers.add_parser(
        'import-owui',
        help='Import Open-WebUI accounts by copying their bcrypt password hashes')
    import_parser.add_argument('--dry-run', action='store_true',
                               help='Report what would happen without changing anything')
    import_parser.set_defaults(func=user_import_owui)

    list_parser = user_subparsers.add_parser('list', help='List accounts')
    list_parser.set_defaults(func=user_list)

    invitations_parser = user_subparsers.add_parser(
        'invitations', help='List outstanding invitations')
    invitations_parser.set_defaults(func=user_invitations)


def user_add(args):
    from ..utils import authentik_manager
    result = authentik_manager.add_user(args.email, args.name)
    print(f"Created user {result['email']} (pk {result['pk']})")


def user_invite(args):
    from ..utils import authentik_manager
    url = authentik_manager.invite_user(args.email, args.name, args.days)
    print(f"Invitation for {args.email} (expires in {args.days}d, single use):")
    print(f"  {url}")


def user_admin(args):
    from ..utils import authentik_manager
    print(authentik_manager.set_admin(args.email, args.remove))


def user_recovery(args):
    from ..utils import authentik_manager
    url = authentik_manager.recovery_link(args.email)
    print(f"Password (re)set link for {args.email} (one-time):")
    print(f"  {url}")


def user_uninvite(args):
    from ..utils import authentik_manager
    count = authentik_manager.revoke_invitation(args.email)
    if count:
        print(f"Revoked {count} invitation(s) for {args.email}")
    else:
        print(f"No outstanding invitation for {args.email}")


def user_import_owui(args):
    from ..utils import authentik_manager
    fmt = "{:<40} {:<14} {}"
    for r in authentik_manager.import_owui_users(dry_run=args.dry_run):
        print(fmt.format(r['email'], r['action'], r['detail']))


def user_list(args):
    from ..utils import authentik_manager
    fmt = "{:<40} {:<26} {:<8} {:<7} {:<6} {}"
    print(fmt.format('EMAIL', 'NAME', 'TYPE', 'ACTIVE', 'ADMIN', 'LAST LOGIN'))
    for u in authentik_manager.users_data():
        print(fmt.format(u['email'], u['name'], u['type'],
                         'yes' if u['active'] else 'no',
                         'yes' if u['is_admin'] else '',
                         u['last_login'] or 'never'))


def user_invitations(args):
    from ..utils import authentik_manager
    fmt = "{:<40} {:<28} {}"
    print(fmt.format('EMAIL', 'EXPIRES', 'URL'))
    for inv in authentik_manager.invitations_data():
        print(fmt.format(inv['email'], inv['expires'] or '-', inv['url']))
