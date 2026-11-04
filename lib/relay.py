#!/usr/bin/python3
"""Loopback-only TCP dispatcher for isolated WARP proxies (no direct fallback)."""
import asyncio
import itertools
import logging
import sys

BACKENDS = [(host, 40000) for host in sys.argv[1:]] or [("10.203.41.2", 40000), ("10.203.42.2", 40000)]
rotation = itertools.count()


async def pump(reader, writer):
    try:
        while data := await reader.read(262144):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def handle(reader, writer, fixed=None):
    selected = next(rotation) % len(BACKENDS) if fixed is None else fixed
    upstream = None
    try:
        # Retry the other WARP backend only on the combined endpoint.
        candidates = [(selected + offset) % len(BACKENDS) for offset in range(len(BACKENDS))] if fixed is None else [selected]
        for index in candidates:
            try:
                remote, upstream = await asyncio.wait_for(
                    asyncio.open_connection(*BACKENDS[index]), timeout=5
                )
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            return
        logging.info("port=%s backend=%s", writer.get_extra_info("sockname")[1], index + 1)
        await asyncio.gather(pump(reader, upstream), pump(remote, writer))
    finally:
        writer.close()
        if upstream:
            upstream.close()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    servers = []
    for port, fixed in [(1080, None)] + [(40001 + i, i) for i in range(len(BACKENDS))]:
        servers.append(await asyncio.start_server(
            lambda r, w, f=fixed: handle(r, w, f), "127.0.0.1", port
        ))
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
