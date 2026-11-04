#!/usr/bin/python3
"""Create the complete WARP aggregation setup, or restore the preceding connections."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from engine import BASE, STATE, start, stop, status, run
from traffic import traffic

MANIFEST = BASE / 'manager-state.json'
VIRTUAL = 'wlan-test'
PREFIX = 'WARP-Aggregate-'


def persist(state):
    temp = MANIFEST.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2))
    temp.chmod(0o600)
    temp.replace(MANIFEST)


def nm(*args, check=True, interactive=False, timeout=50):
    if interactive:
        result = subprocess.run(['nmcli', '--ask', *args], timeout=timeout)
        if check and result.returncode:
            raise RuntimeError('NetworkManager connection failed; original profiles are preserved')
        return ''
    return run('nmcli', *args, check=check, timeout=timeout)


def devices():
    result = []
    for line in nm('-t', '-f', 'DEVICE,TYPE,STATE', 'device').splitlines():
        name, kind, status = line.split(':', 2)
        path = Path('/sys/class/net') / name
        result.append({'name': name, 'kind': kind, 'state': status,
                       'usb': '/usb' in str((path / 'device').resolve()),
                       'phy': str((path / 'phy80211').resolve()) if (path / 'phy80211').exists() else None})
    return result


def current_uuid(interface):
    value = nm('-g', 'GENERAL.CON-UUID', 'device', 'show', interface, check=False)
    return value if value and value != '--' else None


def wifi_profiles():
    profiles = []
    for line in nm('-t', '--escape', 'no', '-f', 'UUID,TYPE,NAME', 'connection', 'show').splitlines():
        uuid, kind, name = line.split(':', 2)
        if kind == '802-11-wireless' and not name.startswith(PREFIX):
            profiles.append((uuid, name))
    return profiles


def choose_profile(interface, requested):
    profiles = wifi_profiles()
    if requested:
        match = next((uuid for uuid, name in profiles if requested in (uuid, name)), None)
        if match:
            return match
        # A new SSID: nmcli handles password entry securely in the terminal.
        nm('device', 'wifi', 'connect', requested, 'ifname', interface, interactive=True, timeout=75)
        return current_uuid(interface)
    preferred = current_uuid(interface)
    print('\nChoose the saved Wi-Fi connection to use for the Wi-Fi paths:')
    for index, (uuid, name) in enumerate(profiles, 1):
        print(f'  {index}. {name}' + (' (current)' if uuid == preferred else ''))
    print('  n. Connect to a new Wi-Fi network (password entered through nmcli)')
    answer = input('Number, or Enter to keep the current connection: ').strip()
    if not answer and preferred:
        return preferred
    if answer.lower() == 'n':
        return choose_profile(interface, input('Wi-Fi network name (SSID): ').strip())
    if answer.isdigit() and 1 <= int(answer) <= len(profiles):
        return profiles[int(answer) - 1][0]
    raise RuntimeError('No Wi-Fi connection selected')


def snapshot_interface(state, interface):
    if interface not in state['interfaces']:
        state['interfaces'][interface] = {'connection': current_uuid(interface)}
        persist(state)


def connect_wifi_clone(state, source_uuid, interface, role, metric):
    snapshot_interface(state, interface)
    name = PREFIX + role + '-' + state['id']
    state['created_profiles'].append(name)
    persist(state)
    nm('connection', 'clone', source_uuid, name)
    nm('connection', 'modify', name,
       'connection.interface-name', interface, 'connection.autoconnect', 'no',
       '802-11-wireless.mac-address', '',
       '802-11-wireless.cloned-mac-address', 'preserve' if role in ('virtual', 'primary') else 'permanent',
       'ipv4.method', 'auto', 'ipv4.addresses', '', 'ipv4.gateway', '',
       'ipv4.never-default', 'no', 'ipv4.may-fail', 'no', 'ipv4.dhcp-client-id', 'mac', 'ipv4.route-metric', str(metric),
       'ipv6.method', 'auto', 'ipv6.addresses', '', 'ipv6.gateway', '',
       'ipv6.route-metric', str(metric))
    nm('--wait', '40', 'connection', 'up', name, 'ifname', interface, interactive=True, timeout=60)


def ensure_ipv4(interface):
    print(f'Checking IPv4 DHCP on {interface}...', flush=True)
    for _ in range(60):
        addresses = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', interface))
        routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'default', 'dev', interface))
        if addresses and any(a.get('scope') == 'global' for a in addresses[0].get('addr_info', [])) and any('gateway' in r for r in routes):
            return
        time.sleep(0.5)
    raise RuntimeError(f'{interface} did not acquire an IPv4 address and gateway; check DHCP or reconnect its Wi-Fi profile')


def off():
    state = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    stop()
    if not state:
        print('WARP paths stopped. No complete-setup manifest to restore.')
        return
    errors = []
    for interface, before in reversed(list(state['interfaces'].items())):
        if not (Path('/sys/class/net') / interface).exists():
            continue
        try:
            if interface == VIRTUAL and state.get('created_virtual'):
                nm('device', 'disconnect', interface, check=False)
                run('iw', 'dev', interface, 'del')
            elif before['connection']:
                nm('--wait', '30', 'connection', 'up', before['connection'], 'ifname', interface)
            else:
                nm('device', 'disconnect', interface, check=False)
        except Exception as error:
            errors.append(f'{interface}: {error}')
    for profile in state['created_profiles']:
        nm('connection', 'delete', profile, check=False)
    if state.get('host_warp'):
        before = state['host_warp']
        run('warp-cli', '--accept-tos', 'mode', before['mode'], check=False)
        run('warp-cli', '--accept-tos', 'connect' if before['connected'] else 'disconnect', check=False)
    if not state.get('warp_service_was_active', True):
        run('systemctl', 'stop', 'warp-svc', check=False)
    state['stopped_at'] = time.time()
    (BASE / 'manager-last-state.json').write_text(json.dumps(state, indent=2))
    if errors:
        state['cleanup_errors'] = errors
        persist(state)
        raise RuntimeError('Tunnels stopped, but some connections need attention: ' + '; '.join(errors))
    MANIFEST.unlink()
    print('Aggregation off. Script-created virtual adapter and profiles removed; prior physical connections and host WARP state restored.')


def on(options):
    found = devices()
    internal = [d for d in found if d['kind'] == 'wifi' and not d['usb'] and d['name'] != VIRTUAL]
    usb_wifi = [d for d in found if d['kind'] == 'wifi' and d['usb']]
    phones = [d for d in found if d['kind'] == 'ethernet' and d['usb']]
    if not internal:
        raise RuntimeError('No internal Wi-Fi adapter detected')
    primary = options.primary or next((d['name'] for d in internal if d['state'] == 'connected'), internal[0]['name'])
    mode = options.mode
    if mode is None:
        print('WARP aggregation:\n  2. Two Wi-Fi identities on the internal adapter\n  3. Internal adapter + virtual identity + USB Wi-Fi\n  phone. Internal pair + USB tethering (+ USB Wi-Fi when present)\n  auto. All supported available paths (up to four)')
        mode = input('Mode [auto]: ').strip() or 'auto'
    if mode not in ('2', '3', 'phone', 'auto'):
        raise RuntimeError('Choose mode 2, 3, phone or auto')
    if mode == '3' and not usb_wifi:
        raise RuntimeError('USB Wi-Fi adapter not found. Use --mode 2, or plug in the dongle.')
    if mode == 'phone' and not phones:
        raise RuntimeError('No USB tethering interface found. Connect the phone and enable USB tethering. The existing setup was not changed.')
    # Check hardware before replacing a working configuration.
    if MANIFEST.exists():
        off()
    elif STATE.exists():
        stop()
    state = {'id': secrets.token_hex(4), 'started_at': time.time(), 'mode': mode,
             'interfaces': {}, 'created_profiles': [], 'created_virtual': False,
             'warp_service_was_active': run('systemctl', 'is-active', 'warp-svc', check=False) == 'active',
             'automatic_rollback': False}
    persist(state)
    try:
        snapshot_interface(state, primary)
        if (Path('/sys/class/net') / VIRTUAL).exists():
            snapshot_interface(state, VIRTUAL)
            nm('device', 'disconnect', VIRTUAL, check=False)
        profile = choose_profile(primary, options.wifi)
        if not profile:
            raise RuntimeError('Could not select a Wi-Fi connection')
        bound = nm('-g', 'connection.interface-name', 'connection', 'show', profile)
        if bound and bound != '--' and bound != primary:
            connect_wifi_clone(state, profile, primary, 'primary', 100)
        else:
            nm('--wait', '40', 'connection', 'up', profile, 'ifname', primary, interactive=True, timeout=60)
        if not (Path('/sys/class/net') / VIRTUAL).exists():
            state['created_virtual'] = True
            state['interfaces'][VIRTUAL] = {'connection': None}
            persist(state)
            run('iw', 'dev', primary, 'interface', 'add', VIRTUAL, 'type', 'managed')
            nm('device', 'set', VIRTUAL, 'managed', 'no')
            run('ip', 'link', 'set', 'dev', VIRTUAL, 'down')
            address = bytearray(secrets.token_bytes(6)); address[0] = (address[0] | 2) & 254
            run('ip', 'link', 'set', 'dev', VIRTUAL, 'address', ':'.join(f'{b:02x}' for b in address))
        nm('device', 'set', VIRTUAL, 'managed', 'yes')
        connect_wifi_clone(state, profile, VIRTUAL, 'virtual', 500)
        specs = [('a', primary, '10.203.41', 40101, 1101), ('b', VIRTUAL, '10.203.42', 40102, 1102)]
        if usb_wifi and mode in ('3', 'phone', 'auto'):
            interface = usb_wifi[0]['name']
            usb_profile = next((u for u, n in wifi_profiles() if options.usb_wifi in (u, n)), None) if options.usb_wifi else profile
            if not usb_profile:
                raise RuntimeError('USB Wi-Fi profile not found; pass a saved profile name with --usb-wifi')
            connect_wifi_clone(state, usb_profile, interface, 'usb-wifi', 600)
            specs.append(('c', interface, '10.203.43', 40104, 1103))
        if phones and mode in ('phone', 'auto'):
            interface = options.phone_interface or phones[0]['name']
            if interface not in [d['name'] for d in phones]:
                raise RuntimeError('Requested phone interface is not a detected USB Ethernet device')
            snapshot_interface(state, interface)
            name = PREFIX + 'phone-' + state['id']
            state['created_profiles'].append(name); persist(state)
            nm('connection', 'add', 'type', 'ethernet', 'ifname', interface, 'con-name', name,
               'connection.autoconnect', 'no', 'ipv4.method', 'auto', 'ipv4.may-fail', 'no', 'ipv4.route-metric', '700')
            nm('--wait', '40', 'connection', 'up', name, 'ifname', interface, timeout=50)
            index = len(specs)
            specs.append(('abcd'[index], interface, f'10.203.{41 + index}', 40104 if index == 2 else 40105, 1101 + index))
        for _, interface, *_ in specs:
            ensure_ipv4(interface)
        run('systemctl', 'start', 'warp-svc')
        settings = json.loads(run('warp-cli', '--accept-tos', '--json', 'settings'))['settings']
        state['host_warp'] = {'mode': settings['operation_mode'],
                              'connected': 'Connected' in run('warp-cli', '--accept-tos', 'status')}
        state['specs'] = specs; persist(state)
        run('warp-cli', '--accept-tos', 'mode', 'doh')
        run('warp-cli', '--accept-tos', 'connect')
        for _ in range(25):
            if 'Connected' in run('warp-cli', '--accept-tos', 'status'):
                try:
                    run('getent', 'ahostsv4', 'www.cloudflare.com', timeout=5)
                    break
                except Exception:
                    pass
            time.sleep(1)
        else:
            raise RuntimeError('The bootstrap encrypted DNS service did not become ready')
        print(f'Building {len(specs)} isolated WARP paths; details: {BASE / "last-on.log"}', flush=True)
        with (BASE / 'last-on.log').open('w') as log, contextlib.redirect_stdout(log):
            start('h2-only', specs=specs)
            traffic()
        # Verify forwarded device traffic, rather than only the daemon's status.
        for lane, interface, *_ in specs:
            trace = run('curl', '-4', '--interface', f'wdxh-{lane}', '--max-time', '15', '-fsS',
                        'https://www.cloudflare.com/cdn-cgi/trace')
            if 'warp=on' not in trace:
                raise RuntimeError(f'Path {lane} via {interface} did not pass WARP verification')
            print(f'VERIFIED {lane}: {interface}, warp=on', flush=True)
        state['ready_at'] = time.time(); persist(state)
        print(f'Aggregation ON: {len(specs)} paths, HTTPS traffic + DNS. No automatic rollback. Motrix jobs were not resumed or changed.', flush=True)
    except BaseException:
        off()
        raise


if __name__ == '__main__':
    if os.geteuid() != 0:
        sys.exit('Use warp-on.sh / warp-off.sh; they request sudo when necessary.')
    if BASE.stat().st_uid != 0:
        sys.exit('Run install.sh first; the privileged backend must be installed in a root-owned directory.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['on', 'off', 'status'])
    parser.add_argument('--mode', choices=['2', '3', 'phone', 'auto'])
    parser.add_argument('--wifi', help='Saved connection name/UUID, or a new SSID')
    parser.add_argument('--usb-wifi', help='Different saved connection for the USB Wi-Fi adapter')
    parser.add_argument('--primary', help='Override internal Wi-Fi device detection')
    parser.add_argument('--phone-interface', help='Override USB tethering device detection')
    options = parser.parse_args()
    BASE.mkdir(mode=0o755, exist_ok=True)
    with (BASE / 'manager.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('Another aggregation On/Off operation is already running.')
        try:
            if options.action == 'on':
                on(options)
            elif options.action == 'off':
                off()
            else:
                status()
        except KeyboardInterrupt:
            sys.exit('Cancelled.')
        except Exception as error:
            sys.exit(f'Error: {error}')
