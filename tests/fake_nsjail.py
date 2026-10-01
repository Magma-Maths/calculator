#!/usr/bin/env python3
"""Fake nsjail for integration tests.

Gives the command after "--" the environment the real jail would: the
caller's environment is dropped unless nsjail.cfg says keep_env: true, and
only the cfg's envar lines and the --env flags remain. The command is then
run and resolved against that PATH, so a bare name that only resolves
through the host PATH fails here exactly as it does inside the jail, and its
exit is logged as nsjail logs it, and a death by a signal becomes exit
status 128 + signal.
Namespaces, mounts, resource limits and seccomp are not simulated; those
flags are accepted and ignored. When FAKE_NSJAIL_RECORD names a file, the
flags before "--" are written there as JSON.
"""
import json
import os
import re
import signal
import sys
from pathlib import Path

# The executor passes --config /app/nsjail.cfg, where the Dockerfile puts this file.
NSJAIL_CFG = Path(__file__).resolve().parent.parent / "nsjail.cfg"


def jail_environment(flags: list[str]) -> dict[str, str]:
    cfg = NSJAIL_CFG.read_text()
    env = dict(os.environ) if re.search(r"^keep_env:\s*true", cfg, re.M) else {}
    for name, value in re.findall(r'^envar:\s*"([^=]+)=(.*)"', cfg, re.M):
        env[name] = value
    for i, flag in enumerate(flags):
        if flag in ("-E", "--env"):
            name, _, value = flags[i + 1].partition("=")
            env[name] = value
    return env


def jail_mounts() -> list[dict[str, str]]:
    """The cfg's mount blocks, one {field: value} dict each, values unquoted."""
    cfg = NSJAIL_CFG.read_text()
    return [
        dict(re.findall(r'^\s*(\w+):\s*"?([^"\n]*?)"?\s*$', body, re.M))
        for body in re.findall(r"^mount\s*\{(.*?)^\}", cfg, re.M | re.S)
    ]


def main() -> None:
    args = sys.argv[1:]
    split = args.index("--")
    flags, command = args[:split], args[split + 1:]
    record = os.environ.get("FAKE_NSJAIL_RECORD")
    if record:
        Path(record).write_text(json.dumps(flags))
    env = jail_environment(flags)
    pid = os.fork()
    if pid == 0:
        try:
            os.execvpe(command[0], command, env)
        except OSError as e:
            print(f"[E] execve('{command[0]}') failed: {e.strerror}", file=sys.stderr)
            os._exit(255)
    status = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    if status < 0:
        sig = -status
        print(
            f"[I][fake] pid={pid} ([STANDALONE MODE]) terminated with signal: "
            f"{signal.Signals(sig).name} ({sig}), (PIDs left: 0)",
            file=sys.stderr,
        )
        sys.exit(128 + sig)
    print(
        f"[I][fake] pid={pid} ([STANDALONE MODE]) exited with status: {status}, (PIDs left: 0)",
        file=sys.stderr,
    )
    sys.exit(status)


if __name__ == "__main__":
    main()
