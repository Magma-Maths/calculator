"""Host side of a Firecracker vsock connection.

Firecracker exposes the guest's AF_VSOCK listeners through one UNIX socket.
The host connects to it, writes "CONNECT <port>\\n" and gets "OK <n>\\n"
once the guest accepted. Until the guest has booted the socket file does not
exist or the connect is refused, so we retry until the caller's deadline.
"""
import asyncio
import os
import socket
import stat
import struct
import time


class VsockError(Exception):
    pass


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass  # peer already reset the socket


async def connect(uds_path: str, port: int, deadline: float, expected_uid: int | None = None):
    last = "socket not present"
    while time.monotonic() < deadline:
        try:
            st = os.lstat(uds_path)
        except OSError:
            st = None
        if st is not None and stat.S_ISLNK(st.st_mode):
            # A symlink at the UDS path cannot be something the guest put
            # there, so this is not a transient boot condition: fail now
            # instead of retrying until the deadline.
            raise VsockError("vsock path is a symlink")
        if st is not None and stat.S_ISSOCK(st.st_mode):
            try:
                reader, writer = await asyncio.open_unix_connection(uds_path)
            except OSError as exc:
                last = f"connect failed: {exc}"
            else:
                writer.write(f"CONNECT {port}\n".encode())
                await writer.drain()
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    await _close(writer)
                    raise VsockError("handshake timed out")
                if line.startswith(b"OK "):
                    if expected_uid is not None:
                        # A compromised firecracker could repoint the UDS
                        # path at a root-owned socket; refuse a peer that
                        # isn't the uid we staged this jail for.
                        sock = writer.get_extra_info("socket")
                        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                        _pid, peer_uid, _gid = struct.unpack("3i", creds)
                        if peer_uid != expected_uid:
                            await _close(writer)
                            raise VsockError("vsock peer uid mismatch")
                    return reader, writer
                await _close(writer)
                if not line:
                    last = "guest closed the handshake (agent not listening yet)"
                else:
                    # A non-OK reply can be transient (e.g. the agent is still
                    # starting up and another listener answered), so retry
                    # instead of failing the whole connect attempt.
                    last = f"unexpected handshake reply: {line!r}"
        await asyncio.sleep(0.2)
    raise VsockError(f"vsock connect deadline reached: {last}")
