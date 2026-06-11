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

    recovery_parser = user_subparsers.add_parser(
        'recovery', help='Issue a one-time password (re)set link for an existing account')
    recovery_parser.add_argument('email', help='Account email')
    recovery_parser.set_defaults(func=user_recovery)

    uninvite_parser = user_subparsers.add_parser(
        'uninvite', help='Revoke outstanding invitation(s) for an email')
    uninvite_parser.add_argument('email', help='Invited email')
    uninvite_parser.set_defaults(func=user_uninvite)

    list_parser = user_subparsers.add_parser('list', help='List accounts')
    list_parser.set_defaults(func=user_list)

    invitations_parser = user_subparsers.add_parser(
        'invitations', help='List outstanding invitations')
    invitations_parser.set_defaults(func=user_invitations)


def user_add(args):
    from ..utils import authentik_manager
    authentik_manager.add_user(args.email, args.name)


def user_invite(args):
    from ..utils import authentik_manager
    authentik_manager.invite_user(args.email, args.name, args.days)


def user_recovery(args):
    from ..utils import authentik_manager
    authentik_manager.recovery_link(args.email)


def user_uninvite(args):
    from ..utils import authentik_manager
    authentik_manager.revoke_invitation(args.email)


def user_list(args):
    from ..utils import authentik_manager
    authentik_manager.list_users()


def user_invitations(args):
    from ..utils import authentik_manager
    authentik_manager.list_invitations()
