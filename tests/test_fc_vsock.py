import asyncio
import os
import socket
import time

import pytest

from firecracker.host import vsock


async def _fake_firecracker(path, reply=b"OK 1024\n", delay=0.0):
    async def handle(reader, writer):
        line = await reader.readline()
        assert line == b"CONNECT 52\n"
        writer.write(reply)
        await writer.drain()
        writer.write(b"payload")
        await writer.drain()
        writer.close()

    await asyncio.sleep(delay)
    return await asyncio.start_unix_server(handle, path=path)


def test_connect_waits_for_socket_then_handshakes(tmp_path):
    path = str(tmp_path / "vsock.sock")

    async def run():
        server_task = asyncio.create_task(_fake_firecracker(path, delay=0.5))
        reader, writer = await vsock.connect(path, 52, deadline=time.monotonic() + 5)
        data = await reader.read(100)
        writer.close()
        server = await server_task
        server.close()
        await server.wait_closed()
        return data

    assert asyncio.run(run()) == b"payload"


def test_connect_times_out_when_socket_never_appears(tmp_path):
    path = str(tmp_path / "never.sock")

    async def run():
        with pytest.raises(vsock.VsockError):
            await vsock.connect(path, 52, deadline=time.monotonic() + 0.6)

    asyncio.run(run())


def test_connect_rejects_symlink_uds_path(tmp_path):
    real_path = str(tmp_path / "real.sock")
    link_path = str(tmp_path / "vsock.sock")

    async def run():
        server = await _fake_firecracker(real_path)
        os.symlink(real_path, link_path)
        start = time.monotonic()
        with pytest.raises(vsock.VsockError) as exc_info:
            await vsock.connect(link_path, 52, deadline=start + 5)
        elapsed = time.monotonic() - start
        server.close()
        await server.wait_closed()
        assert "symlink" in str(exc_info.value)
        assert elapsed < 1

    asyncio.run(run())


def test_connect_accepts_matching_peer_uid(tmp_path):
    path = str(tmp_path / "vsock.sock")

    async def run():
        server = await _fake_firecracker(path)
        reader, writer = await vsock.connect(path, 52, deadline=time.monotonic() + 5, expected_uid=os.getuid())
        data = await reader.read(100)
        writer.close()
        server.close()
        await server.wait_closed()
        return data

    assert asyncio.run(run()) == b"payload"


def test_connect_rejects_mismatched_peer_uid(tmp_path):
    path = str(tmp_path / "vsock.sock")

    async def run():
        server = await _fake_firecracker(path)
        try:
            with pytest.raises(vsock.VsockError) as exc_info:
                await vsock.connect(path, 52, deadline=time.monotonic() + 2, expected_uid=os.getuid() + 1)
        finally:
            server.close()
            await server.wait_closed()
        assert "peer uid" in str(exc_info.value)

    asyncio.run(run())


def test_connect_rejects_bad_handshake(tmp_path):
    path = str(tmp_path / "vsock.sock")

    async def run():
        server = await _fake_firecracker(path, reply=b"ERROR\n")
        start = time.monotonic()
        with pytest.raises(vsock.VsockError) as exc_info:
            await vsock.connect(path, 52, deadline=start + 1)
        elapsed = time.monotonic() - start
        server.close()
        await server.wait_closed()
        assert "unexpected handshake reply" in str(exc_info.value)
        assert elapsed >= 0.8

    asyncio.run(run())


def test_connect_retries_past_connection_refused(tmp_path):
    path = str(tmp_path / "vsock.sock")
    # A listening socket that exits without unlinking leaves a socket file
    # that refuses new connections, the same as a crashed firecracker.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)
    stale.listen(1)
    stale.close()

    async def run():
        async def replace_with_real_server():
            await asyncio.sleep(0.4)
            os.unlink(path)
            return await _fake_firecracker(path)

        server_task = asyncio.create_task(replace_with_real_server())
        reader, writer = await vsock.connect(path, 52, deadline=time.monotonic() + 5)
        data = await reader.read(100)
        writer.close()
        server = await server_task
        server.close()
        await server.wait_closed()
        return data

    assert asyncio.run(run()) == b"payload"
