"""Read-only host discovery. Never grants networking, probes peers, or starts services."""
import json
import re
import subprocess


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('Host inspection unavailable: ' + args[0])
    return result.stdout


def default_uplink():
    routes = json.loads(command('ip', '-j', '-4', 'route', 'show', 'default'))
    routes = [r for r in routes if r.get('dev') and r.get('type', 'unicast') == 'unicast'
              and not r.get('nexthops') and 'linkdown' not in r.get('flags', [])]
    if not routes:
        raise ValueError('No usable IPv4 default route')
    metric = min(int(r.get('metric', 0)) for r in routes)
    devices = {r['dev'] for r in routes if int(r.get('metric', 0)) == metric}
    if len(devices) != 1:
        raise ValueError('Ambiguous equal-priority default routes; explicit uplink required')
    device = devices.pop()
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', device) or device in ('lo', 'solana-host'):
        raise ValueError('Invalid default-route interface')
    links = json.loads(command('ip', '-j', 'link', 'show', 'dev', device))
    if len(links) != 1 or 'UP' not in links[0].get('flags', []):
        raise ValueError('Default-route interface is down')
    return device


def inspect():
    report = {'schema': 'freya-mainnet-preflight-v1', 'uplink': None,
              'missing': [], 'services': {}, 'network_test_run': False,
              'defaults': {'sessionBytes': 1000000, 'sessionSeconds': 55,
                           'uploadKbytes': 16, 'downloadKbytes': 16,
                           'reserveSession': False, 'peerPorts': None, 'calendarMonthly': True,
                           'normalMonthlyBytes': 120000000000, 'hardMonthlyBytes': 150000000000},
              'scope': 'host discovery only; not authorization or full diagnostic validation'}
    try:
        report['uplink'] = default_uplink()
    except (OSError, ValueError, subprocess.SubprocessError):
        report['missing'].append('unambiguous accessible default-route interface')
    for name in ('freya-peer-discovery.service', 'solana-budget-network.service'):
        try:
            raw = command('systemctl', 'show', name, '--no-pager',
                          '--property=LoadState,ActiveState,User,Group,NetworkNamespacePath,ReadWritePaths')
            values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            report['services'][name] = values
            if values.get('LoadState') != 'loaded':
                report['missing'].append(name + ' installation/configuration')
            elif name == 'freya-peer-discovery.service':
                if values.get('User') in (None, '', 'root'):
                    report['missing'].append('configured non-root collector identity')
                if not values.get('ReadWritePaths'):
                    report['missing'].append('configured collector storage and shared ledger access')
                if values.get('NetworkNamespacePath') != '/run/netns/solana-budget':
                    report['missing'].append('managed collector network namespace')
        except (OSError, ValueError, subprocess.SubprocessError):
            report['missing'].append(name + ' inspectable host configuration')
    report['host_discovery_passed'] = not report['missing']
    report['diagnostic_authorized'] = False  # Requires existing budget/config validation, not a ping.
    return report


def main():
    report = inspect()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['host_discovery_passed'] else 2)


if __name__ == '__main__':
    main()
