"""Smoke-test client for the supervisor socket. Run as a user in the magma-api group."""
import argparse
import asyncio
import json
import sys

from app.magma_cmd import wrap_magma_code
from firecracker import protocol


async def run(socket_path: str, code: str, timeout: int, output_bytes: int) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await protocol.write_frame(writer, {"code": wrap_magma_code(code, timeout), "timeout": timeout, "cpu_timeout": timeout, "output_bytes": output_bytes})
        return await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)
    finally:
        await protocol.close_writer(writer)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--socket", default="/run/magma-fc/supervisor.sock")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--output-bytes", type=int, default=20 * 1024)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--code-file")
    g.add_argument("-e", "--code")
    args = p.parse_args()
    code = args.code if args.code is not None else open(args.code_file, encoding="utf-8").read()
    reply = asyncio.run(run(args.socket, code, args.timeout, args.output_bytes))
    print(json.dumps(reply, indent=2))
    return 2 if "error" in reply else 0


if __name__ == "__main__":
    sys.exit(main())
