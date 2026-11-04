#!/usr/bin/python3
"""Reversible, runtime-only multi-path WARP setup. Run the installed root-owned copy."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

BASE = Path(__file__).resolve().parent
RUN = Path("/run/warp-dual-test")
STATE = RUN / "state.json"
def active_lanes():
    state = json.loads(STATE.read_text())
    return [spec for spec in state['specs'] if spec[0] in state['lanes']]


def run(*args, check=True, timeout=40, capture=True):
    result = subprocess.run([str(a) for a in args], text=True,
                            capture_output=capture, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stderr or result.stdout}")
    return result.stdout.strip() if capture else result.returncode


def save(state):
    STATE.write_text(json.dumps(state, indent=2))


def cli(lane, *args, check=True, timeout=40):
    pid = run("systemctl", "show", f"warp-dual-{lane}", "-p", "MainPID", "--value")
    if not pid.isdigit() or int(pid) <= 1:
        raise RuntimeError(f"WARP daemon {lane} is not running")
    if os.stat(f"/proc/{pid}/ns/net").st_ino != os.stat(f"/run/netns/wdx-{lane}").st_ino:
        raise RuntimeError("Daemon has not entered its private network namespace")
    runtime = Path(f"/proc/{pid}/root/run/cloudflare-warp")
    if not runtime.samefile(BASE / lane / "run"):
        raise RuntimeError("Daemon has not entered its private mount namespace")
    # Enter both namespaces: the private Unix socket must match the private daemon.
    return run("nsenter", "-t", pid, "-m", "-n", "--", "/usr/bin/warp-cli",
               "--accept-tos", "--no-ansi", *args, check=check, timeout=timeout)


def unit(name, command, properties=()):
    args = ["systemd-run", "--quiet", "--collect", f"--unit={name}",
            "--property=Type=exec", "--property=TimeoutStopSec=10", "--property=KillMode=control-group"]
    for prop in properties:
        args.append(f"--property={prop}")
    run(*args, "--", *command)


def isolate_arp(state):
    """Keep same-subnet uplinks from answering ARP for one another's IPs."""
    settings = state.setdefault('arp_settings', {})
    for _, interface, *_ in state['specs']:
        for key in ('arp_ignore', 'arp_announce'):
            name = f'net.ipv4.conf.{interface}.{key}'
            settings.setdefault(name, run('sysctl', '-n', name))
    save(state)
    for _, interface, *_ in state['specs']:
        run('sysctl', '-q', '-w', f'net.ipv4.conf.{interface}.arp_ignore=1',
            f'net.ipv4.conf.{interface}.arp_announce=2')
    # Correct upstream neighbor entries that may already point at another NIC.
    for _, interface, *_ in state['specs']:
        data = json.loads(run('ip', '-j', '-4', 'addr', 'show', 'dev', interface))
        address = next(a['local'] for a in data[0]['addr_info'] if a['scope'] == 'global')
        run('arping', '-U', '-I', interface, '-c', '2', '-w', '2', address,
            check=False, timeout=5)


def stop():
    if not STATE.exists():
        print("Aggregation is already off.", flush=True)
        return
    state = json.loads(STATE.read_text())
    from connectivity import remove_probe_routes
    remove_probe_routes(state)
    if state.get("traffic"):
        # Remove the device-wide policy before stopping either tunnel.
        for family in ("-4", "-6"):
            run("ip", family, "rule", "del", "pref", "1150", "lookup", "40103", check=False)
            run("ip", family, "route", "flush", "table", "40103", check=False)
        if "ipv6_hash" in state:
            run("sysctl", "-q", "-w", f"net.ipv6.fib_multipath_hash_policy={state['ipv6_hash']}")
    units = ["warp-dual-relay", "warp-dual-rollback.timer"]
    for lane in state["lanes"]:
        units += [f"warp-dual-{lane}{suffix}" for suffix in ("", "-export", "-dns-udp", "-dns-tcp")]
    run("systemctl", "stop", *units, check=False, timeout=90)
    for lane, wifi, prefix, table, priority in state['specs']:
        if lane not in state.get("lanes", []):
            continue
        run("ip", "rule", "del", "pref", str(priority), "iif", f"wdxh-{lane}",
            "lookup", str(table), check=False)
        run("ip", "rule", "del", "pref", str(priority + 10), "to", f"{prefix}.0/30",
            "lookup", "main", check=False)
        run("ip", "route", "flush", "table", str(table), check=False)
        run("firewall-cmd", "--zone=trusted", f"--remove-interface=wdxh-{lane}", check=False)
        run("ip", "link", "del", f"wdxh-{lane}", check=False)
        run("ip", "netns", "del", f"wdx-{lane}", check=False)
        shutil.rmtree(Path("/etc/netns") / f"wdx-{lane}", ignore_errors=True)
    run("nft", "delete", "table", "ip", "warp_dual_test", check=False)
    if state.get("forward_changed"):
        run("sysctl", "-q", "-w", f"net.ipv4.ip_forward={state['ip_forward']}")
    for name, value in state.get('arp_settings', {}).items():
        run('sysctl', '-q', '-w', f'{name}={value}', check=False)
    if state.get('ipv4_hash_changed'):
        run('sysctl', '-q', '-w', f"net.ipv4.fib_multipath_hash_policy={state['ipv4_hash']}")
    state["stopped_at"] = time.time()
    (BASE / "last-state.json").write_text(json.dumps(state, indent=2))
    STATE.unlink()
    print("Aggregation services, namespaces, routes and firewall additions removed.", flush=True)
    print("Original Wi-Fi connections and host WARP configuration were preserved.", flush=True)


def start(protocol, specs):
    if STATE.exists():
        raise RuntimeError("Aggregation is already active. Use the On/Off launcher to rebuild it.")
    selected = specs
    if not selected or len(selected) > 4 or len({s[3] for s in selected}) != len(selected) or any(s[3] == 40103 for s in selected):
        raise RuntimeError('Invalid or conflicting path specifications')
    for lane, wifi, prefix, table, priority in selected:
        interfaces = json.loads(run("ip", "-j", "link", "show"))
        names = {i["ifname"] for i in interfaces}
        if wifi not in names or f"wdxh-{lane}" in names:
            raise RuntimeError(f"Missing {wifi} or conflicting aggregation interface")
        if any(n.split()[0] == f"wdx-{lane}" for n in run("ip", "netns", "list").splitlines()):
            raise RuntimeError("Conflicting namespace")
        if run("ip", "route", "show", "table", str(table), check=False):
            raise RuntimeError(f"Routing table {table} is already used")
        existing_rules = json.loads(run("ip", "-j", "rule"))
        if any(r.get("priority") in (priority, priority + 10) for r in existing_rules):
            raise RuntimeError("Routing rule priority already used")
        existing_routes = json.loads(run("ip", "-j", "route", "show", "table", "all"))
        import ipaddress
        target = ipaddress.ip_network(f"{prefix}.0/30")
        for route in existing_routes:
            dst = route.get("dst", "default")
            if dst != "default" and target.overlaps(ipaddress.ip_network(dst, strict=False)):
                raise RuntimeError(f"Reserved aggregation subnet overlaps {dst}")
    import socket
    for port in [1080] + list(range(40001, 40001 + len(selected))):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = {"started_at": time.time(), "protocol": protocol, "lanes": [], "specs": selected, "auto_rollback": False,
             "ip_forward": run("sysctl", "-n", "net.ipv4.ip_forward"),
             'ipv4_hash': run('sysctl', '-n', 'net.ipv4.fib_multipath_hash_policy')}
    # Changing ip_forward can reset interface defaults; capture originals first.
    state['arp_settings'] = {
        f'net.ipv4.conf.{interface}.{key}': run('sysctl', '-n', f'net.ipv4.conf.{interface}.{key}')
        for _, interface, *_ in selected for key in ('arp_ignore', 'arp_announce')
    }
    snapshot = BASE / f"snapshot-{int(time.time())}"
    snapshot.mkdir(mode=0o700)
    for name, args in {
        "routes.json": ["ip", "-j", "-4", "route", "show", "table", "all"],
        "rules.json": ["ip", "-j", "-4", "rule"],
        "addresses.json": ["ip", "-j", "address"],
        "firewall.nft": ["nft", "list", "ruleset"],
        "host-warp-settings.json": ["warp-cli", "--accept-tos", "--json", "settings"],
        "host-warp-status.txt": ["warp-cli", "--accept-tos", "status"],
        "connections.txt": ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device"],
    }.items():
        (snapshot / name).write_text(run(*args))
    state["snapshot"] = str(snapshot)
    save(state)
    try:
        # No scheduled rollback: this setup runs until manual cleanup or reboot.
        if state['ipv4_hash'] != '1':
            state['ipv4_hash_changed'] = True
            save(state)
            run('sysctl', '-q', '-w', 'net.ipv4.fib_multipath_hash_policy=1')
        if state["ip_forward"] != "1":
            state["forward_changed"] = True
            save(state)
            run("sysctl", "-q", "-w", "net.ipv4.ip_forward=1")
        isolate_arp(state)
        run("nft", "add", "table", "ip", "warp_dual_test")
        run("nft", "add", "chain", "ip", "warp_dual_test", "postrouting",
            "{ type nat hook postrouting priority srcnat - 5; policy accept; }")
        for lane, wifi, prefix, table, priority in selected:
            state["lanes"].append(lane)
            save(state)
            data = json.loads(run("ip", "-j", "-4", "addr", "show", "dev", wifi))
            address = next(a["local"] for a in data[0]["addr_info"] if a["scope"] == "global")
            routes = json.loads(run("ip", "-j", "-4", "route", "show", "default", "dev", wifi))
            gateway = next(r["gateway"] for r in routes if "gateway" in r)
            ns, host = f"wdx-{lane}", f"wdxh-{lane}"
            run("ip", "netns", "add", ns)
            dnsdir = Path("/etc/netns") / ns
            dnsdir.mkdir(parents=True, mode=0o755)
            (dnsdir / "resolv.conf").write_text(f"nameserver {prefix}.1\noptions timeout:2 attempts:2\n")
            (dnsdir / "nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\nnetworks: files\n")
            run("ip", "link", "add", host, "type", "veth", "peer", "name", "eth0", "netns", ns)
            run("ip", "addr", "add", f"{prefix}.1/30", "dev", host)
            run("ip", "link", "set", host, "up")
            run("ip", "-n", ns, "link", "set", "lo", "up")
            run("ip", "-n", ns, "addr", "add", f"{prefix}.2/30", "dev", "eth0")
            run("ip", "-n", ns, "link", "set", "eth0", "up")
            run("ip", "-n", ns, "route", "add", "default", "via", f"{prefix}.1")
            run("ip", "rule", "add", "pref", str(priority), "iif", host, "lookup", str(table))
            run("ip", "rule", "add", "pref", str(priority + 10), "to", f"{prefix}.0/30", "lookup", "main")
            run("ip", "route", "add", "table", str(table), f"{gateway}/32", "dev", wifi, "scope", "link")
            run("ip", "route", "add", "table", str(table), "default", "via", gateway, "dev", wifi, "src", address)
            # An unreachable default prevents fallback to the other Wi-Fi when a route disappears.
            run("ip", "route", "add", "table", str(table), "unreachable", "default", "metric", "32760")
            run("firewall-cmd", "--zone=trusted", f"--add-interface={host}")
            run("nft", "add", "rule", "ip", "warp_dual_test", "postrouting", "ip", "saddr",
                f"{prefix}.0/30", "oifname", wifi, "counter", "masquerade")
            # Bootstrap through the host's existing encrypted Cloudflare DNS resolver.
            # These listeners bind only the experiment's private veth addresses.
            unit(f"warp-dual-{lane}-dns-udp", ["/usr/bin/socat",
                 f"UDP4-RECVFROM:53,bind={prefix}.1,reuseaddr,fork", "UDP4:127.0.2.2:53"],
                 ["Restart=on-failure"])
            unit(f"warp-dual-{lane}-dns-tcp", ["/usr/bin/socat",
                 f"TCP4-LISTEN:53,bind={prefix}.1,reuseaddr,fork", "TCP4:127.0.2.2:53"],
                 ["Restart=on-failure"])
            print(f"{lane}: underlay pinned to {wifi} ({address})", flush=True)
            print(run("ip", "netns", "exec", ns, "curl", "-4", "--max-time", "15", "-fsS",
                      "https://www.cloudflare.com/cdn-cgi/trace"), flush=True)
            lane_dir = BASE / lane
            for part in ("state", "run", "log"):
                (lane_dir / part).mkdir(parents=True, mode=0o755, exist_ok=True)
            # The daemon may unmount a bind-mounted resolv.conf during DNS recovery.
            # Give it an entire private /etc so recovery cannot expose host DNS files.
            etc = lane_dir / "etc"
            etc.mkdir(mode=0o755, exist_ok=True)
            for filename in ("pki", "ssl", "crypto-policies", "passwd", "group",
                             "hosts", "services", "protocols", "localtime", "os-release"):
                source, destination = Path("/etc") / filename, etc / filename
                if source.exists() and not destination.exists():
                    if source.is_dir():
                        shutil.copytree(source, destination, symlinks=True)
                    else:
                        shutil.copy2(source, destination)
            (etc / "resolv.conf").write_text((dnsdir / "resolv.conf").read_text())
            (etc / "nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\nnetworks: files\n")
            if not (etc / "machine-id").exists():
                import uuid
                (etc / "machine-id").write_text(uuid.uuid4().hex + "\n")
            props = [f"NetworkNamespacePath=/run/netns/{ns}", "PrivateMounts=yes",
                     "TemporaryFileSystem=/run:rw",
                     f"BindPaths={lane_dir}/state:/var/lib/cloudflare-warp {lane_dir}/run:/run/cloudflare-warp {lane_dir}/log:/var/log/cloudflare-warp {etc}:/etc",
                     "Restart=on-failure", "RestartSec=3"]
            (lane_dir / "run/warp_service").unlink(missing_ok=True)
            unit(f"warp-dual-{lane}", ["/usr/bin/warp-svc"], props)
            for _ in range(30):
                try:
                    cli(lane, "status", timeout=3)
                    break
                except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError(f"Daemon {lane} did not create its private socket")
            if not (lane_dir / "state/reg.json").exists():
                print(f"{lane}: registering an independent WARP instance", flush=True)
                print(cli(lane, "registration", "new", timeout=60), flush=True)
            print(cli(lane, "tunnel", "protocol", "set", "MASQUE"), flush=True)
            print(cli(lane, "tunnel", "masque-options", "set", protocol), flush=True)
            print(cli(lane, "mode", "proxy"), flush=True)
            print(cli(lane, "proxy", "port", "40000"), flush=True)
            print(cli(lane, "connect"), flush=True)
            unit(f"warp-dual-{lane}-export", ["/usr/bin/socat",
                 f"TCP4-LISTEN:40000,bind={prefix}.2,reuseaddr,fork", "TCP4:127.0.0.1:40000"],
                 [f"NetworkNamespacePath=/run/netns/{ns}", "Restart=on-failure"])
        unit("warp-dual-relay", ["/usr/bin/python3", str(BASE / "relay.py"), *[f"{spec[2]}.2" for spec in selected]],
             ["DynamicUser=yes", "ProtectSystem=strict", "ProtectHome=yes",
              "NoNewPrivileges=yes", "Restart=on-failure",
              "ReadOnlyPaths=/var/lib/warp-dual-test/relay.py"])
        print(f"Configured {len(selected)} WARP paths; proxy-stage combined endpoint: 127.0.0.1:1080.", flush=True)
        print("No automatic rollback. Use stop to restore the original network, or reboot.", flush=True)
    except BaseException:
        stop()
        raise


def status():
    if not STATE.exists():
        print("No active experiment.")
        return
    print(STATE.read_text())
    for lane, *_ in active_lanes():
        print(f"\nWARP {lane}:")
        try:
            print(cli(lane, "status"))
            settings = cli(lane, "--json", "settings")
            parsed = json.loads(settings)
            print(json.dumps({k: v for k, v in parsed.get("settings", parsed).items()
                              if any(x in k for x in ("mode", "protocol", "proxy", "masque"))}, indent=2))
        except Exception as error:
            print(error)
    print(run("nft", "list", "table", "ip", "warp_dual_test", check=False))
