import asyncio
import socket
import struct

import pytest

from firecracker import protocol


def _pair():
    a, b = socket.socketpair()
    a.settimeout(2)
    b.settimeout(2)
    return a, b


def test_pack_roundtrip():
    payload = protocol.pack({"code": "1+1;", "n": 3})
    assert struct.unpack(">I", payload[:4])[0] == len(payload) - 4
    assert protocol.unpack(payload[4:]) == {"code": "1+1;", "n": 3}


def test_send_recv_frame():
    a, b = _pair()
    protocol.send_frame(a, {"stdout": "2\n"})
    assert protocol.recv_frame(b, max_bytes=1024) == {"stdout": "2\n"}


def test_recv_frame_rejects_oversize_before_reading_body():
    a, b = _pair()
    a.sendall(struct.pack(">I", 10_000_000))
    with pytest.raises(protocol.FrameError):
        protocol.recv_frame(b, max_bytes=1024)


def test_recv_frame_rejects_zero_length():
    a, b = _pair()
    a.sendall(struct.pack(">I", 0))
    with pytest.raises(protocol.FrameError):
        protocol.recv_frame(b, max_bytes=1024)


def test_recv_frame_on_closed_connection():
    a, b = _pair()
    a.close()
    with pytest.raises(protocol.FrameError):
        protocol.recv_frame(b, max_bytes=1024)


def test_unpack_rejects_non_dict():
    with pytest.raises(protocol.FrameError):
        protocol.unpack(b"[1,2]")
    with pytest.raises(protocol.FrameError):
        protocol.unpack(b"not json")


def test_async_roundtrip():
    async def run():
        server_side = {}

        async def handle(reader, writer):
            server_side["req"] = await protocol.read_frame(reader, max_bytes=1024)
            await protocol.write_frame(writer, {"ok": True})
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await protocol.write_frame(writer, {"hello": 1})
        reply = await protocol.read_frame(reader, max_bytes=1024)
        writer.close()
        server.close()
        await server.wait_closed()
        return server_side["req"], reply

    req, reply = asyncio.run(run())
    assert req == {"hello": 1}
    assert reply == {"ok": True}


def test_async_read_frame_rejects_oversize():
    async def run():
        async def handle(reader, writer):
            writer.write(struct.pack(">I", 5_000_000))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        with pytest.raises(protocol.FrameError):
            await protocol.read_frame(reader, max_bytes=1024)
        writer.close()
        server.close()
        await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("value", [10**400, float("inf"), float("nan"), -1, -0.5])
def test_bounded_cpu_time_sec_rejects_out_of_range(value):
    assert protocol.bounded_cpu_time_sec(value) is None


@pytest.mark.parametrize("value", ["5", None, True, False, [1], {"a": 1}])
def test_bounded_cpu_time_sec_rejects_wrong_type(value):
    assert protocol.bounded_cpu_time_sec(value) is None


def test_bounded_cpu_time_sec_accepts_sane_values():
    assert protocol.bounded_cpu_time_sec(0) == 0.0
    assert protocol.bounded_cpu_time_sec(1.5) == 1.5
    assert protocol.bounded_cpu_time_sec(protocol.MAX_CPU_TIME_SEC) == float(protocol.MAX_CPU_TIME_SEC)
    assert protocol.bounded_cpu_time_sec(protocol.MAX_CPU_TIME_SEC + 1) is None


@pytest.mark.parametrize("value", [10**400, float("inf"), float("nan"), -1, 12.5, "5", None, True, False])
def test_bounded_memory_kb_rejects_invalid(value):
    # Float is rejected outright, even a finite in-range one: real rusage
    # memory is always a whole number of KiB.
    assert protocol.bounded_memory_kb(value) is None


def test_bounded_memory_kb_accepts_sane_values():
    assert protocol.bounded_memory_kb(0) == 0
    assert protocol.bounded_memory_kb(20480) == 20480
    assert protocol.bounded_memory_kb(protocol.MAX_MEMORY_KB) == protocol.MAX_MEMORY_KB
    assert protocol.bounded_memory_kb(protocol.MAX_MEMORY_KB + 1) is None
