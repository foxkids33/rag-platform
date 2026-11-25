"""Carry TLS between the RAG API and corporate Confluence over the active Cisco tunnel."""

import asyncio
import os
from pathlib import Path
import socket
from contextlib import suppress

HOST = os.environ["RAG_CONFLUENCE_HOST"]
SOCKET = Path("/transport/confluence.sock")
SLOTS = asyncio.Semaphore(32)


async def copy(reader, writer):
    while chunk := await asyncio.wait_for(reader.read(65536), timeout=90):
        writer.write(chunk)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def handle(reader, writer):
    upstream = None
    tasks = []
    sock = None
    try:
        async with SLOTS:
            loop = asyncio.get_running_loop()
            addr = (await asyncio.wait_for(loop.getaddrinfo(
                HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM), timeout=5))[0][4]
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"cscotun0\0")
            sock.setblocking(False)
            await asyncio.wait_for(loop.sock_connect(sock, addr), timeout=6)
            incoming, upstream = await asyncio.open_connection(sock=sock)
            sock = None
            tasks = [asyncio.create_task(copy(reader, upstream)),
                     asyncio.create_task(copy(incoming, writer))]
            await asyncio.gather(*tasks)
    except (OSError, TimeoutError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if sock is not None:
            sock.close()
        for stream in (upstream, writer):
            if stream is not None:
                stream.close()
                with suppress(OSError):
                    await stream.wait_closed()


async def main():
    os.umask(0o077)
    SOCKET.unlink(missing_ok=True)
    async with await asyncio.start_unix_server(handle, path=str(SOCKET), backlog=32) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
