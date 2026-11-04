#!/usr/bin/python3
"""Allow only device-bound Fedora Internet probes around the aggregate route."""
import json
import socket
from pathlib import Path
from engine import STATE, active_lanes, run, save

HOST = 'fedoraproject.org'
URI = 'http://fedoraproject.org/static/hotspot.txt'


def remove_probe_routes(state):
    for rule in state.get('probe_rules', []):
        args = ['ip', '-4', 'rule', 'del', 'pref', str(rule['priority'])]
        # Linux can retain an oif rule after a USB interface disappears. Its old
        # name can no longer be resolved, so use the other owned selectors then.
        if (Path('/sys/class/net') / rule['interface']).exists():
            args += ['oif', rule['interface']]
        run(*args, 'to', rule['destination'], 'lookup', str(rule['table']), check=False)
    state['probe_rules'] = []


def install_probe_routes():
    state = json.loads(STATE.read_text())
    remove_probe_routes(state)
    save(state)
    addresses = sorted({r[4][0] for r in socket.getaddrinfo(HOST, 80, socket.AF_INET, socket.SOCK_STREAM)})
    existing = json.loads(run('ip', '-4', '-j', 'rule'))
    for index, (_, interface, _, table, _) in enumerate(active_lanes(), 1):
        priority = 1120 + index
        if any(r.get('priority') == priority for r in existing):
            raise RuntimeError(f'Connectivity rule priority {priority} is in use')
        for address in addresses:
            rule = {'priority': priority, 'interface': interface,
                    'destination': address + '/32', 'table': table}
            state['probe_rules'].append(rule)
            save(state)
            run('ip', '-4', 'rule', 'add', 'pref', str(priority), 'oif', interface,
                'to', rule['destination'], 'lookup', str(table))
    state['probe_uri'] = URI
    save(state)
    print(run('nmcli', 'networking', 'connectivity', 'check', check=False, timeout=35), flush=True)


if __name__ == '__main__':
    install_probe_routes()
