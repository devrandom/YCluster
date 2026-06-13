"""GPU VM hosting and per-user SSH access management."""

from ..utils import vm_manager as vm


def register_vm_commands(subparsers):
    p = subparsers.add_parser('vm', help='GPU VM hosting (Incus)')
    p.set_defaults(func=lambda args: args.parser.print_help(), parser=p)
    sub = p.add_subparsers(dest='vm_command', help='vm commands')

    lp = sub.add_parser('launch', help='Launch a GPU VM or container')
    lp.add_argument('name')
    lp.add_argument('--owner', required=True,
                    help='Registered user who owns (and can SSH to) the instance')
    lp.add_argument('--gpus', type=int, default=1,
                    help='VM: number of GPUs to pass through (0 = none). '
                         'Container: 0 to detach the shared GPU, anything '
                         '>0 keeps it (single shared GPU per host).')
    lp.add_argument('--cpu', type=int, default=8)
    lp.add_argument('--mem', default='32GiB')
    lp.add_argument('--type', dest='instance_type',
                    choices=['auto', 'vm', 'container'], default='auto',
                    help="Instance type. 'auto' (default) picks per-host: "
                         "vm on VM hosts (NVIDIA passthrough), container on "
                         "container hosts (AMD shared GPU).")
    lp.add_argument('--image', default=None,
                    help='Override the default image for the chosen type.')
    lp.add_argument('--bill', action='store_true',
                    help='Bill this runtime to the owner (CLI operations '
                         'default to non-billable admin/debug time)')
    lp.set_defaults(func=lambda a: vm.vm_launch(
        a.name, a.owner, a.gpus, a.cpu, a.mem, a.image, a.instance_type,
        billable=a.bill))

    lsp = sub.add_parser('list', help='List registered VMs')
    lsp.set_defaults(func=lambda a: vm.vm_list())

    sp = sub.add_parser('stop', help='Stop a VM')
    sp.add_argument('name')
    sp.add_argument('--bill', action='store_true',
                    help='Mark as billable (owner-requested) rather than admin/debug')
    sp.add_argument('--release', action='store_true',
                    help="Also detach the VM's passthrough GPUs back to the "
                         "host pool (next start re-acquires them). Default: "
                         "keep them attached for a fast restart.")
    sp.set_defaults(func=lambda a: vm.vm_stop(a.name, billable=a.bill,
                                              release=a.release))

    stp = sub.add_parser('start', help='Start a stopped VM')
    stp.add_argument('name')
    stp.add_argument('--bill', action='store_true',
                    help='Bill this runtime to the owner (CLI default: non-billable)')
    stp.set_defaults(func=lambda a: vm.vm_start(a.name, billable=a.bill))

    rsp = sub.add_parser('restart', help='Stop (if running) and start a VM')
    rsp.add_argument('name')
    rsp.add_argument('--bill', action='store_true',
                    help='Bill the continued runtime to the owner')
    rsp.set_defaults(func=lambda a: vm.vm_restart(a.name, billable=a.bill))

    dp = sub.add_parser('destroy', help='Delete a VM and its registration')
    dp.add_argument('name')
    dp.set_defaults(func=lambda a: vm.vm_destroy(a.name))

    rp = sub.add_parser('resize', help="Grow a VM's root disk")
    rp.add_argument('name')
    rp.add_argument('size', help='New disk size, e.g. 160GiB')
    rp.set_defaults(func=lambda a: vm.vm_resize(a.name, a.size))

    gp = sub.add_parser('gpus', help='Show passthrough GPU allocation')
    gp.set_defaults(func=lambda a: vm.vm_gpus())

    rgp = sub.add_parser(
        'release-gpus',
        help="Detach a stopped VM's passthrough GPUs back to the host "
             "pool (the next start re-acquires its registered count)")
    rgp.add_argument('name')
    rgp.set_defaults(func=lambda a: print(
        f"Released {vm.release_gpus(a.name)} GPU(s) from '{a.name}'."))

    pip = sub.add_parser(
        'pin-ips',
        help='Pin a static IP on every existing VM/container (migration '
             'helper; new launches pin automatically).')
    pip.set_defaults(func=lambda a: _print_pins(vm.pin_existing_vms()))

    smp = sub.add_parser(
        'sample', help='Snapshot local incus state to etcd (vm-state-sampler timer)')
    smp.set_defaults(func=lambda a: vm.sample_state())

    rcp = sub.add_parser(
        'reconcile', help='Converge local instances to desired power state (vm-reconciler timer)')
    rcp.set_defaults(func=lambda a: vm.reconcile())

    sdp = sub.add_parser(
        'sync-dns',
        help='Reconcile bridge dnsmasq host-records with the pinned '
             'instance IPs (launch/destroy/pin-ips sync automatically; '
             'run this after manual incus changes or to backfill).')
    sdp.set_defaults(func=lambda a: _print_dns_sync(vm.sync_dns_records()))

    bp = sub.add_parser('bastion-sync',
                        help='Regenerate the bastion SSH access list from etcd')
    bp.set_defaults(func=lambda a: vm.bastion_sync())

    bwp = sub.add_parser('bastion-watch',
                         help='Long-running: re-sync the bastion on every '
                              'user/VM registry change (systemd service)')
    bwp.set_defaults(func=lambda a: vm.bastion_watch())

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


def _print_dns_sync(changed):
    if not changed:
        print("No changes — every bridge's host-records already match "
              "the pins.")
        return
    for bridge, records in sorted(changed.items()):
        print(f"{bridge}: {len(records)} host-record(s)")
        for r in records:
            print(f"  {r}")


def _print_pins(changes):
    if not changes:
        print("No changes — every instance on this host is already pinned "
              "(or only the bastion is present).")
        return
    print(f"Pinned {len(changes)} instance(s):")
    for name, ip, note in changes:
        print(f"  {name:<24} {ip:<16}  ({note})")
    print("\nThe pin takes effect on next DHCP renewal (≈1h) or on the "
          "next instance restart.")
    print("Restart now for immediate effect plus to activate "
          "port_isolation / ipv4_filtering from the updated profile.")
