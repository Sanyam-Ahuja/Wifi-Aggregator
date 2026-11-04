#!/usr/bin/python3
"""Show live receive/transmit rates for Wi-Fi and USB Ethernet devices, without sudo."""
import argparse
from pathlib import Path
import time


def sample():
    result = {}
    for interface in Path('/sys/class/net').iterdir():
        wifi = (interface / 'wireless').exists()
        usb = '/usb' in str((interface / 'device').resolve())
        if not wifi and not (usb and (interface / 'type').read_text().strip() == '1'):
            continue
        try:
            result[interface.name] = tuple(int((interface / 'statistics' / key).read_text())
                                           for key in ('rx_bytes', 'tx_bytes'))
        except OSError:
            pass
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=0, help='Zero means run until Ctrl-C')
    parser.add_argument('--interval', type=float, default=1)
    options = parser.parse_args()
    before, started, count = sample(), time.monotonic(), 0
    try:
        while options.samples == 0 or count < options.samples:
            time.sleep(options.interval)
            now, ended = sample(), time.monotonic()
            elapsed = ended - started
            print('\n' + time.strftime('%H:%M:%S') + '  Physical traffic, including tunnel overhead')
            total = 0
            for name, counters in now.items():
                old = before.get(name, counters)
                rx, tx = (max(0, counters[i] - old[i]) * 8 / elapsed / 1e6 for i in (0, 1))
                total += rx
                print(f'{name:<20} RX {rx:7.2f} Mbps   TX {tx:7.2f} Mbps')
            print(f'Total RX: {total:.2f} Mbps', flush=True)
            before, started, count = now, ended, count + 1
    except KeyboardInterrupt:
        pass
