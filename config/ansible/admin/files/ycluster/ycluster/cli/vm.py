"""GPU VM hosting and per-user SSH access management."""

from ..utils import vm_manager as vm


def register_vm_commands(subparsers):
    p = subparsers.add_parser('vm', help='GPU VM hosting (Incus)')
    p.set_defaults(func=lambda args: args.parser.print_help(), parser=p)
    sub = p.add_subparsers(dest='vm_command', help='vm commands')

    lp = sub.add_parser('launch', help='Launch a GPU-passthrough VM')
    lp.add_argument('name')
    lp.add_argument('--owner', required=True,
                    help='Registered user who owns (and can SSH to) the VM')
    lp.add_argument('--gpus', type=int, default=1)
    lp.add_argument('--cpu', type=int, default=8)
    lp.add_argument('--mem', default='32GiB')
    lp.add_argument('--image', default=vm.GPU_VM_IMAGE)
    lp.set_defaults(func=lambda a: vm.vm_launch(
        a.name, a.owner, a.gpus, a.cpu, a.mem, a.image))

    lsp = sub.add_parser('list', help='List registered VMs')
    lsp.set_defaults(func=lambda a: vm.vm_list())

    sp = sub.add_parser('stop', help='Stop a VM')
    sp.add_argument('name')
    sp.set_defaults(func=lambda a: vm.vm_stop(a.name))

    stp = sub.add_parser('start', help='Start a stopped VM')
    stp.add_argument('name')
    stp.set_defaults(func=lambda a: vm.vm_start(a.name))

    dp = sub.add_parser('destroy', help='Delete a VM and its registration')
    dp.add_argument('name')
    dp.set_defaults(func=lambda a: vm.vm_destroy(a.name))

    gp = sub.add_parser('gpus', help='Show passthrough GPU allocation')
    gp.set_defaults(func=lambda a: vm.vm_gpus())

    bp = sub.add_parser('bastion-sync',
                        help='Regenerate the bastion SSH access list from etcd')
    bp.set_defaults(func=lambda a: vm.bastion_sync())

    # --- ssh key registry ---
    ssh_p = sub.add_parser('ssh', help='Manage per-user SSH keys for VM access')
    ssh_p.set_defaults(func=lambda a: a.parser.print_help(), parser=ssh_p)
    ssh_sub = ssh_p.add_subparsers(dest='vm_ssh_command')

    sap = ssh_sub.add_parser('add', help="Register a user's SSH public key")
    sap.add_argument('user')
    sap.add_argument('key', nargs='+', help='SSH public key (may be unquoted)')
    sap.set_defaults(func=_ssh_add)

    slp = ssh_sub.add_parser('list', help="List users, or one user's keys")
    slp.add_argument('user', nargs='?')
    slp.set_defaults(func=lambda a: vm.user_list(a.user))

    srp = ssh_sub.add_parser('remove', help="Remove a user's key (substring match)")
    srp.add_argument('user')
    srp.add_argument('key', help='Key text or a unique substring to match')
    srp.set_defaults(func=_ssh_remove)


def _ssh_add(args):
    vm.user_add_key(args.user, ' '.join(args.key))
    vm.vm_sync_keys(args.user)
    vm.bastion_sync()


def _ssh_remove(args):
    vm.user_remove_key(args.user, args.key)
    vm.vm_sync_keys(args.user)
    vm.bastion_sync()
