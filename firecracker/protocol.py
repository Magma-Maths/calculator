"""Length-prefixed JSON frames shared by the host supervisor and the guest agent.

A frame is a 4-byte big-endian length followed by that many bytes of UTF-8
JSON encoding one object. The length is validated against a caller-supplied
maximum before any body bytes are read, so a hostile peer cannot make the
reader allocate more than that maximum.
"""
import asyncio
import json
import socket
import struct

AGENT_PORT = 52
MAX_REQUEST_BYTES = 256 * 1024
MAX_REPLY_BYTES = 1024 * 1024
# Room the supervisor leaves for the environment and limits it adds to the guest request.
GUEST_REQUEST_OVERHEAD = 16 * 1024
CODE_TOO_LARGE = "code too large"

_HEADER = struct.Struct(">I")


class FrameError(Exception):
    pass


def encode(obj) -> bytes:
    """The frame body for obj; JSON escaping can make it several times the raw text."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def code_fits(code: str) -> bool:
    """Whether the supervisor accepts code of this size; measured encoded, as escaping can grow it sixfold."""
    return len(encode(code)) <= MAX_REQUEST_BYTES - GUEST_REQUEST_OVERHEAD


def pack(obj: dict) -> bytes:
    body = encode(obj)
    return _HEADER.pack(len(body)) + body


def fit_reply(reply: dict, max_bytes: int | None = None) -> dict:
    """Shorten stdout, then stderr, until the encoded reply fits in one frame.

    Output caps count raw bytes, but escaping a control character takes six
    bytes, so a reply within its caps can still overflow MAX_REPLY_BYTES.
    """
    if max_bytes is None:
        max_bytes = MAX_REPLY_BYTES
    if len(encode(reply)) <= max_bytes:
        return reply
    reply = dict(reply, truncated=True)
    for key in ("stdout", "stderr"):
        text = reply.get(key)
        if not isinstance(text, str):
            continue
        reply[key] = ""
        if len(encode(reply)) > max_bytes:
            continue
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            reply[key] = text[:mid]
            if len(encode(reply)) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        reply[key] = text[:lo]
        break
    return reply


def unpack(payload: bytes) -> dict:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError(f"invalid frame body: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameError("frame body is not an object")
    return obj


def _check_length(length: int, max_bytes: int) -> None:
    if length == 0 or length > max_bytes:
        raise FrameError(f"frame length {length} outside 1..{max_bytes}")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise FrameError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket, max_bytes: int) -> dict:
    (length,) = _HEADER.unpack(recv_exact(sock, _HEADER.size))
    _check_length(length, max_bytes)
    return unpack(recv_exact(sock, length))


def send_frame(sock: socket.socket, obj: dict) -> None:
    sock.sendall(pack(obj))


async def read_frame(reader: asyncio.StreamReader, max_bytes: int) -> dict:
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        raise FrameError("connection closed") from exc
    (length,) = _HEADER.unpack(header)
    _check_length(length, max_bytes)
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise FrameError("connection closed") from exc
    return unpack(body)


async def write_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write(pack(obj))
    await writer.drain()
