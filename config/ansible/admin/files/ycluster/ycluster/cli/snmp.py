"""
SNMP access to cluster devices (PDUs and similar) via SNMPv3.

Credentials and device allocations are read from etcd:
  - /cluster/config/pdu/snmpv3-auth
  - /cluster/config/pdu/snmpv3-priv
  - /cluster/nodes/by-hostname/ (entries with type=pdu)

Uses python3-pysnmp4 (apt package) — no external binary required.
"""

import json
import sys

from ..common.etcd_utils import get_etcd_client

SNMP_USER = 'ycluster'
SNMP_PORT = 161


def register_snmp_commands(subparsers):
    p = subparsers.add_parser('snmp', help='SNMPv3 access to cluster devices (PDUs)')
    p.set_defaults(func=lambda args: p.print_help())
    sub = p.add_subparsers(dest='snmp_command', help='snmp commands')

    list_p = sub.add_parser('list', help='List registered devices')
    list_p.set_defaults(func=_cmd_list)

    get_p = sub.add_parser('get', help='SNMP GET one or more OIDs')
    get_p.add_argument('host', help='Device hostname (e.g. pdu1) or IP')
    get_p.add_argument('oids', nargs='+', help='OIDs (numeric, dotted form)')
    get_p.set_defaults(func=_cmd_get)

    walk_p = sub.add_parser('walk', help='SNMP WALK a subtree')
    walk_p.add_argument('host', help='Device hostname (e.g. pdu1) or IP')
    walk_p.add_argument('oid', nargs='?', default='.1',
                        help='OID to walk (default: .1 — the whole tree)')
    walk_p.set_defaults(func=_cmd_walk)


def _cmd_list(args):
    client = get_etcd_client()
    devices = _list_devices(client)
    if not devices:
        print('No SNMP devices registered in etcd')
        return
    for d in devices:
        print(f"{d['hostname']:<12} {d['ip']:<16} type={d.get('type', '?')}")


def _cmd_get(args):
    _run_snmp('get', args.host, args.oids)


def _cmd_walk(args):
    _run_snmp('walk', args.host, [args.oid])


def _run_snmp(operation, host, oids):
    try:
        from pysnmp.hlapi import (
            SnmpEngine, UsmUserData, UdpTransportTarget, ContextData,
            ObjectIdentity, ObjectType, getCmd, nextCmd,
            usmHMACSHAAuthProtocol, usmAesCfb128Protocol,
        )
    except ImportError:
        print("error: pysnmp not available — install python3-pysnmp4",
              file=sys.stderr)
        sys.exit(1)

    client = get_etcd_client()
    device = _resolve_device(client, host)
    if device is None:
        print(f"error: no SNMP device matching '{host}' in etcd",
              file=sys.stderr)
        sys.exit(1)

    auth, priv = _snmp_credentials(client)
    credentials = UsmUserData(SNMP_USER, authKey=auth, privKey=priv,
                              authProtocol=usmHMACSHAAuthProtocol,
                              privProtocol=usmAesCfb128Protocol)
    target = UdpTransportTarget((device['ip'], SNMP_PORT), timeout=5, retries=2)
    varbinds = [ObjectType(ObjectIdentity(oid)) for oid in oids]
    context = ContextData()

    if operation == 'get':
        # pysnmp4's getCmd returns a generator (asyncio-style API)
        for (error_indication, error_status, error_index,
             var_binds) in getCmd(SnmpEngine(), credentials, target,
                                  context, *varbinds):
            _check_snmp_error(error_indication, error_status, error_index)
            for name, value in var_binds:
                print(f'{name.prettyPrint()} = {value.prettyPrint()}')
    else:  # walk
        for (error_indication, error_status, error_index, var_binds) in nextCmd(
                SnmpEngine(), credentials, target, context, *varbinds,
                lexicographicMode=False):
            _check_snmp_error(error_indication, error_status, error_index)
            for name, value in var_binds:
                print(f'{name.prettyPrint()} = {value.prettyPrint()}')


def _check_snmp_error(error_indication, error_status, error_index):
    if error_indication:
        print(f'error: {error_indication}', file=sys.stderr)
        sys.exit(1)
    if error_status:
        print(f'error: {error_status.prettyPrint()} at var-bind index '
              f'{error_index}', file=sys.stderr)
        sys.exit(1)


def _list_devices(client):
    devices = []
    for value, _meta in client.get_prefix('/cluster/nodes/by-hostname/'):
        if not value:
            continue
        alloc = json.loads(value.decode())
        if alloc.get('type') == 'pdu':
            devices.append(alloc)
    devices.sort(key=lambda d: d.get('hostname', ''))
    return devices


def _resolve_device(client, host):
    host = host.removesuffix('.xc')
    for device in _list_devices(client):
        if host in (device.get('hostname'), device.get('ip')):
            return device
    return None


def _snmp_credentials(client):
    auth_val = client.get('/cluster/config/pdu/snmpv3-auth')[0]
    priv_val = client.get('/cluster/config/pdu/snmpv3-priv')[0]
    if auth_val is None or priv_val is None:
        print('error: SNMPv3 credentials missing from etcd '
              '(/cluster/config/pdu/)', file=sys.stderr)
        sys.exit(1)
    decode = lambda v: v.decode() if isinstance(v, bytes) else v
    return decode(auth_val), decode(priv_val)
