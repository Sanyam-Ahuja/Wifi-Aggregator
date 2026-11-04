# WiFi Aggregator

Combine multiple Wi-Fi identities and optional USB phone tethering through parallel Cloudflare WARP tunnels on a Linux laptop.

The tool creates a second station on the internal Wi-Fi radio, connects the chosen networks with NetworkManager, starts an isolated WARP instance per path, and distributes device IPv4 connections before encryption. USB interface names are discovered each run. Two launchers control the complete setup, including virtual-adapter creation after reboot.

Requirements: Linux with systemd, NetworkManager, firewalld, iproute2, `iw`, `arping` (iputils), `nft`, `socat`, Bash and Python 3.10+, plus the official Cloudflare WARP client. The internal Wi-Fi driver must support two simultaneous managed stations. Tested with Fedora Silverblue, an internal MediaTek adapter, a TP-Link USB Wi-Fi adapter and Android USB tethering. This is hardware-aware tooling for that Linux stack, not a driver installer.

Install from the checkout:

```sh
git clone https://github.com/Sanyam-Ahuja/Wifi-Aggregator.git
cd Wifi-Aggregator
./install.sh
```

Installation requests sudo, copies the backend into a root-owned runtime directory, and installs three commands. It does not change connections, stop a running aggregation, install packages or enable startup services.

```sh
warp-on       # choose hardware mode and Wi-Fi in the terminal
warp-off      # stop aggregation and restore the preceding connections
warp-speed    # live rates for Wi-Fi and USB tethering; Ctrl-C to stop monitoring
```

Press Enter at the mode prompt to use `auto`. Then select a saved Wi-Fi connection, or press Enter to keep the current one. For a new network, choose `n`; NetworkManager asks for its password in the terminal. Saved credentials are reused. Passwords are not embedded in scripts or passed as password command-line arguments.

| Mode | Paths |
| --- | --- |
| `2` | Internal Wi-Fi + a virtual station on the same radio |
| `3` | Internal pair + one USB Wi-Fi dongle |
| `phone` | Internal pair + USB tethering; also includes USB Wi-Fi when present |
| `auto` | Available supported paths, up to four |

To skip the menus, supply the mode and saved connection name:

```sh
warp-on --mode 2 --wifi 'My saved Wi-Fi'
warp-on --mode 3 --wifi 'My saved Wi-Fi'
warp-on --mode phone --wifi 'My saved Wi-Fi'
warp-on --mode auto --wifi 'My saved Wi-Fi'
```

Choose one command. Re-running On cleans up the previous setup and rebuilds it for the selected hardware. An explicitly requested missing device is reported before replacing the existing setup. `phone` requires a phone connected by USB with USB tethering enabled.

The Wi-Fi paths use the same selected network by default. A different saved network can be selected for the USB adapter:

```sh
warp-on --mode 3 --wifi 'Internal Wi-Fi profile' --usb-wifi 'USB Wi-Fi profile'
```

USB Wi-Fi and USB Ethernet/tethering devices are detected from their type and USB parent, rather than a fixed interface name. When a device returns under a different name, rerun `warp-on --mode auto`. Advanced overrides are `--primary INTERFACE` and `--phone-interface INTERFACE`. This version selects one USB Wi-Fi adapter and one USB Ethernet device; it does not aggregate an arbitrary number of adapters.

Each path uses WARP traffic-and-DNS mode with MASQUE over HTTPS/TCP. The ordinary desktop WARP instance supplies bootstrap encrypted DNS and may display DNS-only mode while the separate instances carry device traffic. The On command verifies forwarded traffic through each path and prints `VERIFIED … warp=on`.

The setup runs until Off or reboot. There is no timed rollback or automatic hot-plug rebuild. If a path disappears, flows assigned to it can stall; rerun On to rebuild for the connected hardware. There is no automatic tunnel-health failover. Off removes a virtual station created by the tool, deletes its per-run profiles, and restores preceding physical connections, the host WARP state and changed kernel settings. Pre-existing virtual stations are preserved. A failed startup cleans up partial work.

Public IPv6 is unavailable in this version. Local networks retain their original routes. A namespace forwarding filter prevents affected traffic from falling back through plain Wi-Fi when a WARP tunnel disconnects.

To inspect the active configuration:

```sh
sudo python3 /var/lib/warp-dual-test/manager.py status
ip -4 rule
ip -4 route show table 40103
```

`ip route show default` alone shows the main table, rather than the policy table used by aggregation. `warp-speed` detects the current Wi-Fi and USB Ethernet names and reports physical traffic in Mbps, including tunnel overhead. Applications often display MB/s; multiply by eight to compare with Mbps.

More paths do not guarantee a 2×, 3× or 4× speed increase. This balances separate connections, not packets from one connection. Radio airtime, AP backhaul, service limits and remote peers still apply. USB tethering shares whichever Internet connection the phone is using; a phone on the same access point does not add an independent mobile-data uplink.

On and Off do not resume, pause, move or configure download jobs. No download-client integration, local benchmark records, registrations, Wi-Fi secrets, machine snapshots or private runtime data are part of this repository.

The backend keeps the compatibility runtime name `/var/lib/warp-dual-test`, state under `/run/warp-dual-test`, and `warp-dual-*` unit names. These names support up to four paths. Configuration and registration files remain on the laptop when aggregation stops. See [the implementation notes](docs/architecture.md) for routing, isolation and connectivity-check details.
