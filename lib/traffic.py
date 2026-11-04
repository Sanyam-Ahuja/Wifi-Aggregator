#!/usr/bin/python3
"""Switch configured WARP daemons between proxy and device traffic modes."""
import json
from pathlib import Path
import sys
import time
from engine import BASE, STATE, active_lanes, cli, run, save


def proxy():
    state = json.loads(STATE.read_text())
    from connectivity import remove_probe_routes
    remove_probe_routes(state)
    save(state)
    if state.get("traffic"):
        for family in ("-4", "-6"):
            run("ip", family, "rule", "del", "pref", "1150", "lookup", "40103", check=False)
            run("ip", family, "route", "flush", "table", "40103", check=False)
        if "ipv6_hash" in state:
            run("sysctl", "-q", "-w", f"net.ipv6.fib_multipath_hash_policy={state['ipv6_hash']}")
        state["traffic"] = False
        save(state)
    for lane, _, prefix, *_ in active_lanes():
        run("ip", "netns", "exec", f"wdx-{lane}", "nft", "delete", "table", "inet", "warp_dual_forward", check=False)
        print(cli(lane, "mode", "proxy"), flush=True)
        print(cli(lane, "connect"), flush=True)
    print("Returned to dual WARP proxies; original host routes are in use.", flush=True)


def traffic():
    state = json.loads(STATE.read_text())
    if state.get("traffic"):
        raise RuntimeError("Device traffic mode is already active")
    for family in ("-4", "-6"):
        if run("ip", family, "route", "show", "table", "40103", check=False):
            raise RuntimeError("Traffic routing table is already used")
        if any(r.get("priority") == 1150 for r in json.loads(run("ip", family, "-j", "rule"))):
            raise RuntimeError("Traffic rule priority is already used")
    state["traffic"] = True
    state["ipv6_hash"] = run("sysctl", "-n", "net.ipv6.fib_multipath_hash_policy")
    save(state)
    try:
        for lane, wifi, prefix, *_ in active_lanes():
            print(f"{lane}: enabling WARP traffic + DNS over HTTPS/TCP", flush=True)
            print(cli(lane, "mode", "warp+doh"), flush=True)
            print(cli(lane, "connect"), flush=True)
            for _ in range(30):
                if "Connected" in cli(lane, "status") and "CloudflareWARP" in run("ip", "-n", f"wdx-{lane}", "-br", "link"):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"Full WARP {lane} did not become ready")
            ns = f"wdx-{lane}"
            run("ip", "netns", "exec", ns, "sysctl", "-q", "-w", "net.ipv4.ip_forward=1", "net.ipv6.conf.all.forwarding=1")
            run("ip", "netns", "exec", ns, "nft", "add", "table", "inet", "warp_dual_forward")
            run("ip", "netns", "exec", ns, "nft", "add", "chain", "inet", "warp_dual_forward", "postrouting",
                "{ type nat hook postrouting priority srcnat - 5; policy accept; }")
            run("ip", "netns", "exec", ns, "nft", "add", "chain", "inet", "warp_dual_forward", "forward",
                "{ type filter hook forward priority filter - 5; policy drop; }")
            run("ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "warp_dual_forward", "forward",
                "iifname", "eth0", "oifname", "CloudflareWARP", "counter", "accept")
            run("ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "warp_dual_forward", "forward",
                "iifname", "CloudflareWARP", "oifname", "eth0", "ct", "state", "established,related", "counter", "accept")
            run("ip", "netns", "exec", ns, "nft", "add", "rule", "inet", "warp_dual_forward", "postrouting",
                "iifname", "eth0", "oifname", "CloudflareWARP", "counter", "masquerade")
            print(run("ip", "netns", "exec", ns, "curl", "-4", "--max-time", "12", "-fsS",
                      "https://www.cloudflare.com/cdn-cgi/trace"), flush=True)
        # Keep LAN, Tailscale and bootstrap encrypted DNS on their existing routes.
        for network in ("10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                        "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
                        "162.159.36.1/32", "162.159.46.1/32", "1.1.1.1/32", "1.0.0.1/32"):
            run("ip", "-4", "route", "add", "table", "40103", "throw", network)
        for network in ("::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
                        "2606:4700:4700::1111/128", "2606:4700:4700::1001/128"):
            run("ip", "-6", "route", "add", "table", "40103", "throw", network)
        for lane, _, prefix, *_ in active_lanes():
            run("ip", "-4", "route", "add", "table", "40103", f"{prefix}.0/30", "dev", f"wdxh-{lane}", "scope", "link")
        hops = []
        for lane, _, prefix, *_ in active_lanes():
            hops += ["nexthop", "via", f"{prefix}.2", "dev", f"wdxh-{lane}", "weight", "1"]
        run("ip", "-4", "route", "add", "table", "40103", "default", *hops)
        # This campus connection had no IPv6 Internet route before the experiment.
        # Keep public IPv6 unavailable instead of allowing a future native bypass.
        for family in ("-4", "-6"):
            run("ip", family, "route", "add", "table", "40103", "unreachable", "default", "metric", "32760")
        # These rules are the commit point. The physical underlay pinning rules run first.
        for family in ("-4", "-6"):
            run("ip", family, "rule", "add", "pref", "1150", "lookup", "40103")
        for family in ("-4",):
            trace = run("curl", family, "--max-time", "15", "-fsS", "https://www.cloudflare.com/cdn-cgi/trace")
            print(trace, flush=True)
            if "warp=on" not in trace:
                raise RuntimeError(f"Host {family} verification did not use WARP")
        print(f"Device IPv4 traffic now uses {len(active_lanes())} full WARP tunnels; DNS remains encrypted.", flush=True)
        print("Public IPv6 remains unavailable, as on the original connection.", flush=True)
        print("The earlier local SOCKS/HTTP endpoints are inactive in this mode. Apps use their normal connection settings.", flush=True)
        try:
            from connectivity import install_probe_routes
            install_probe_routes()
        except Exception as error:
            print(f"WARP is connected; NetworkManager probe adjustment failed: {error}", flush=True)
    except BaseException:
        proxy()
        raise


if __name__ == "__main__":
    if sys.argv[1:] == ["on"]:
        traffic()
    elif sys.argv[1:] == ["proxy"]:
        proxy()
    else:
        sys.exit("Usage: sudo python3 /var/lib/warp-dual-test/traffic.py on|proxy")
