"""Presentation layers for cluster health checks."""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .check_cluster import (
    INITIAL_ETCD_HOST,
    STORAGE_VIP,
    _d,
    _svc_status_str,
    check_dhcp_service_on_host,
    check_dnsmasq_service_on_host,
    check_etcd_host,
    fetch_cluster_status,
    get_all_nodes,
    get_cluster_members,
    get_etcd_client,
)


def _check_etcd_hosts(mark):
    """Run etcd health checks concurrently and return discovered hosts/results."""
    hosts = get_cluster_members(INITIAL_ETCD_HOST)
    mark('get_cluster_members')
    results = []
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        for result in executor.map(check_etcd_host, hosts):
            results.append(result)
    mark('etcd_health_checks')
    return hosts, results


def _get_node_lists(client):
    """Return all nodes and the non-AMT subset used by service checks."""
    if not client:
        return [], []
    nodes = get_all_nodes(client)
    non_amt_nodes = [
        node for node in nodes
        if node.get('ip') and not node.get('hostname', '').endswith('a')
    ]
    return nodes, non_amt_nodes


def _start_cluster_status_fetch():
    executor = ThreadPoolExecutor(max_workers=1)
    return executor, executor.submit(fetch_cluster_status)


def _print_compact_etcd(results, problems):
    healthy = [result for result in results if result['healthy']]
    unhealthy = [result for result in results if not result['healthy']]
    total = len(results)
    if unhealthy:
        for result in unhealthy:
            problems.append(f"  etcd {result['host']}: {result['error']}")
        print(f"  etcd           {len(healthy)}/{total} healthy")
    else:
        leader = next((r['host'] for r in healthy if r.get('is_leader')), '?')
        print(f"  etcd           {total}/{total} healthy, leader {leader}")


def _print_compact_vip_and_storage(cluster_status, problems):
    error = cluster_status.get('_error')
    vip_info = _d(cluster_status.get('vipStatus'))

    if error:
        print("  vip            unknown (storage VIP unreachable)")
        problems.append(f"  vip: storage VIP ({STORAGE_VIP}) unreachable: {error}")
    else:
        gateway = _d(vip_info.get('gateway_vip'))
        storage = _d(vip_info.get('storage_vip'))
        gateway_host = gateway.get('master_hostname', '?')
        storage_host = storage.get('master_hostname', '?')
        print(
            f"  vip            gateway {gateway_host} ({gateway.get('ip', '?')}), "
            f"storage {storage_host} ({storage.get('ip', '?')})"
        )
        if gateway_host == '?':
            problems.append("  vip: gateway VIP has no master")
        if storage_host == '?':
            problems.append("  vip: storage VIP has no master")

    leadership = _d(cluster_status.get('leadership'))
    storage_leader = leadership.get('storage_leader')
    services = _d(_d(_d(cluster_status.get('hostHealth')).get(storage_leader)).get('services')) if storage_leader else {}
    _print_compact_ceph_and_services(error, storage_leader, services, problems)


def _print_compact_ceph_and_services(error, storage_leader, services, problems):
    if error:
        print("  ceph           unknown (storage VIP unreachable)")
        print("  storage        unknown (storage VIP unreachable)")
    elif not storage_leader:
        print("  ceph           unknown (no storage leader)")
        print("  storage        no leader")
        problems.append("  storage: no storage leader detected")
    else:
        ceph = _d(services.get('ceph'))
        status = ceph.get('status', 'unknown')
        detail = _svc_status_str(ceph.get('details', ''))
        if status == 'healthy':
            print(f"  ceph           {detail}")
        else:
            print(f"  ceph           {status}: {detail}")
            problems.append(f"  ceph: {status} - {detail}")

        service_parts = []
        for service_name, label in [
            ('postgresql', 'pg'),
            ('qdrant', 'qdrant'),
            ('docker_registry', 'registry'),
        ]:
            service = _d(services.get(service_name))
            status = service.get('status', 'unknown')
            if status == 'healthy':
                service_parts.append(f"{label} ok")
            else:
                service_parts.append(f"{label} {status}")
                detail = _svc_status_str(service.get('details', ''))
                problem = f"  storage: {service_name} {status} on {storage_leader}"
                if detail:
                    problem += f" - {detail}"
                problems.append(problem)
        print(f"  storage        leader {storage_leader}, {', '.join(service_parts)}")


def _collect_dhcp_dns(non_amt_nodes, mark):
    """Run DHCP and DNS probes concurrently; DNS only on core nodes."""
    dhcp_results = []
    dhcp_running = []
    dns_results = []

    if non_amt_nodes:
        dns_nodes = [
            node for node in non_amt_nodes
            if re.match(r'^s\d+$', node.get('hostname', ''))
        ]
        with ThreadPoolExecutor(max_workers=10) as executor:
            dhcp_futures = {
                executor.submit(check_dhcp_service_on_host, node['ip']): node
                for node in non_amt_nodes
            }
            dns_futures = {
                executor.submit(check_dnsmasq_service_on_host, node['ip'], node.get('hostname', 'unknown')): node
                for node in dns_nodes
            }

            for future in as_completed(dhcp_futures):
                result = future.result()
                result['_node'] = dhcp_futures[future]
                dhcp_results.append(result)
                if result['running']:
                    dhcp_running.append(result)

            for future in as_completed(dns_futures):
                result = future.result()
                result['_node'] = dns_futures[future]
                dns_results.append(result)
    mark('dhcp_dns_per_node')
    return dhcp_results, dhcp_running, dns_results


def _print_compact_dhcp(running, problems):
    count = len(running)
    if count == 0:
        print("  dhcp           no servers running")
        problems.append("  dhcp: no DHCP servers running in cluster")
    elif count == 1:
        leader_ip = running[0]['host']
        leader_name = running[0]['_node'].get('hostname', leader_ip)
        print(f"  dhcp           ok, leader {leader_name} ({leader_ip})")
    else:
        addresses = ', '.join(result['host'] for result in running)
        print(f"  dhcp           {count} servers running (split brain?)")
        problems.append(f"  dhcp: multiple servers running: {addresses}")


def _print_compact_dns(results, problems):
    dns_ok = []
    dns_warn = []
    for result in results:
        hostname = result['_node'].get('hostname', '?')
        if not re.match(r'^s\d+$', hostname):
            continue
        if result.get('running') and result.get('dns_working'):
            dns_ok.append(hostname)
        elif result.get('running'):
            dns_warn.append(f"  dns {hostname}: {result.get('dns_error', 'resolution failed')}")
        else:
            dns_warn.append(f"  dns {hostname}: not running")

    total = len(dns_ok) + len(dns_warn)
    print(f"  dns            {len(dns_ok)}/{total} core nodes ok")
    problems.extend(dns_warn)


def _print_compact_problems(problems):
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(problem)
    else:
        print("\nNo problems detected.")


def _run_compact(mark):
    problems = []
    print("Cluster Health")

    etcd_hosts, etcd_results = _check_etcd_hosts(mark)
    _print_compact_etcd(etcd_results, problems)

    try:
        client = get_etcd_client(etcd_hosts)
    except Exception:
        client = None
    mark('etcd_client')

    executor, cluster_status_future = _start_cluster_status_fetch()
    nodes, non_amt_nodes = _get_node_lists(client)
    mark('get_all_nodes')

    cluster_status = cluster_status_future.result()
    executor.shutdown(wait=False)
    mark('cluster_status_fetch')
    _print_compact_vip_and_storage(cluster_status, problems)

    dhcp_results, dhcp_running, dns_results = _collect_dhcp_dns(non_amt_nodes, mark)
    _print_compact_dhcp(dhcp_running, problems)
    _print_compact_dns(dns_results, problems)

    visible_nodes = [node for node in nodes if not node.get('hostname', '').endswith('a')]
    print(f"  nodes          {len(visible_nodes)} registered")
    mark('render_output')

    _print_compact_problems(problems)


def _print_verbose_etcd(results):
    print("Host Health Status:")
    print("-" * 60)
    for result in sorted(results, key=lambda item: item['host']):
        if result['healthy']:
            leader = " (LEADER)" if result.get('is_leader') else ""
            print(
                f"✓ {result['host']}: Healthy{leader} "
                f"(v{result['version']}, DB {result['db_size']:,} bytes)"
            )
        else:
            print(f"✗ {result['host']}: Unhealthy - {result['error']}")


def _print_verbose_dhcp(running):
    print("\n" + "=" * 60)
    print("DHCP Service Status:")
    print("-" * 60)
    for result in sorted(running, key=lambda item: item['_node'].get('hostname', '')):
        node = result['_node']
        etcd_status = "✓" if result.get('etcd_connected', False) else "✗"
        print(
            f"✓ {node.get('hostname', 'unknown')} ({result['host']}): DHCP running "
            f"(etcd {etcd_status}, server IP {result.get('server_ip', 'unknown')})"
        )


def _print_verbose_dhcp_summary(results, running):
    count = len(running)
    if count == 0:
        print("⚠ WARNING: No DHCP servers running in cluster!")
        for result in sorted(results, key=lambda item: item['_node'].get('hostname', '')):
            node = result['_node']
            print(f"✗ {node.get('hostname', 'unknown')} ({result['host']}): {result['error']}")
    elif count > 1:
        addresses = ', '.join(result['host'] for result in running)
        print(f"⚠ WARNING: Multiple DHCP servers running: {addresses}")
    else:
        print(f"✓ DHCP leader election working correctly - single server on {running[0]['host']}")


def _print_verbose_dns_result(result):
    node = result['_node']
    if not result.get('running'):
        print(f"✗ {node.get('hostname', 'unknown')} ({result['host']}): DNS not running - {result['error']}")
        return

    hostname = node.get('hostname', 'unknown')
    if result.get('dns_working', False):
        detail = ""
        if 'amt_hostname' in result:
            detail = f", {result['amt_hostname']} -> {result['amt_resolved_ip']}"
        print(
            f"✓ {hostname} ({result['host']}): DNS OK "
            f"({result['hostname']} -> {result['resolved_ip']}{detail})"
        )
    else:
        print(
            f"⚠ {hostname} ({result['host']}): DNS running but: "
            f"{result.get('dns_error', 'Unknown error')}"
        )


def _print_verbose_dns(results):
    print("\n" + "=" * 60)
    print("dnsmasq Service Status:")
    print("-" * 60)
    for result in sorted(results, key=lambda item: item['_node'].get('hostname', '')):
        _print_verbose_dns_result(result)


def _print_verbose_vips(vip_info):
    for vip_name, vip_key in [('Gateway', 'gateway_vip'), ('Storage', 'storage_vip')]:
        vip = _d(vip_info.get(vip_key))
        master = vip.get('master_hostname', 'none')
        ip = vip.get('ip', '?')
        interface = vip.get('interface', '?')
        if master and master != 'none':
            print(f"✓ {vip_name} VIP ({ip}): active on {master} ({interface})")
        else:
            print(f"✗ {vip_name} VIP ({ip}): no master")

    keepalived_nodes = vip_info.get('keepalived_nodes', [])
    if keepalived_nodes:
        running = [node['hostname'] for node in keepalived_nodes if node.get('keepalived_active')]
        stopped = [node['hostname'] for node in keepalived_nodes if not node.get('keepalived_active')]
        message = f"  Keepalived: running on {', '.join(running)}"
        if stopped:
            message += f", stopped on {', '.join(stopped)}"
        print(message)


def _print_verbose_certificate(cluster_status):
    certificate = _d(cluster_status.get('certificateStatus'))
    details = _d(certificate.get('details'))
    days = details.get('days_until_expiry', '?')
    subject = details.get('subject', '?')
    symbol = "✓" if certificate.get('status') == 'healthy' else "⚠"
    print(f"\n  {symbol} TLS certificate ({subject}): expires in {days} days")


def _print_verbose_storage(cluster_status):
    print("\n" + "=" * 60)
    print("Storage & VIP Status:")
    print("-" * 60)

    error = cluster_status.get('_error')
    if error:
        print(f"✗ Storage VIP ({STORAGE_VIP}) unreachable: {error}")
        return

    vip_info = _d(cluster_status.get('vipStatus'))
    _print_verbose_vips(vip_info)

    leadership = _d(cluster_status.get('leadership'))
    storage_leader = leadership.get('storage_leader', 'none')
    dhcp_leader = leadership.get('dhcp_leader', 'none')
    print(f"  Leaders: storage={storage_leader}, dhcp={dhcp_leader}")

    leader_services = _d(_d(_d(cluster_status.get('hostHealth')).get(storage_leader)).get('services'))
    if leader_services:
        print(f"\n  Storage Leader Services ({storage_leader}):")
        for service_name in ['ceph', 'postgresql', 'qdrant', 'docker_registry', 'open_webui']:
            service = _d(leader_services.get(service_name))
            status = service.get('status', 'unknown')
            details = _svc_status_str(service.get('details', ''))
            if status == 'healthy':
                symbol = "✓"
            elif status in ('degraded', 'standby'):
                symbol = "⚠"
            else:
                symbol = "✗"
            detail = f" - {details}" if details else ""
            print(f"  {symbol} {service_name}: {status}{detail}")

    _print_verbose_certificate(cluster_status)


def _print_verbose_nodes(client, nodes):
    print("\n" + "=" * 60)
    print("Registered Nodes:")
    print("-" * 60)

    if not client:
        print("Could not connect to any etcd host to retrieve nodes")
    elif not nodes:
        print("No nodes registered in etcd")
    else:
        nodes.sort(key=lambda node: node.get('hostname', ''))
        print(f"{'Hostname':<10} {'Type':<10} {'IP':<15} {'MAC':<20} {'Allocated'}")
        print("-" * 80)
        for node in nodes:
            allocated = node.get('allocated_at', 'Unknown')
        if allocated != 'Unknown':
            try:
                parsed = datetime.fromisoformat(allocated.replace('Z', '+00:00'))
                allocated = parsed.strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
        print(
            f"{node.get('hostname', 'N/A'):<10} "
            f"{node.get('type', 'N/A'):<10} "
            f"{node.get('ip', 'N/A'):<15} "
            f"{node.get('mac', 'N/A'):<20} "
            f"{allocated}"
        )

    print("=" * 60)


def _run_verbose(mark):
    print("=" * 60)
    print("ETCD Cluster Health Check")
    print("=" * 60)

    etcd_hosts, etcd_results = _check_etcd_hosts(mark)
    print(f"Discovered cluster members: {', '.join(etcd_hosts)}")
    print()
    _print_verbose_etcd(etcd_results)

    try:
        client = get_etcd_client(etcd_hosts)
    except Exception:
        client = None
    mark('etcd_client')

    executor, cluster_status_future = _start_cluster_status_fetch()
    nodes, non_amt_nodes = _get_node_lists(client)

    if client:
        dhcp_results, dhcp_running, dns_results = _collect_dhcp_dns(non_amt_nodes, mark)
        _print_verbose_dhcp(dhcp_running)
        _print_verbose_dhcp_summary(dhcp_results, dhcp_running)
        _print_verbose_dns(dns_results)
    else:
        nodes = []

    cluster_status = cluster_status_future.result()
    executor.shutdown(wait=False)
    mark('cluster_status_fetch')
    _print_verbose_storage(cluster_status)
    _print_verbose_nodes(client, nodes)
