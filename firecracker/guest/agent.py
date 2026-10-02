"""Guest-side job runner. Runs as a child of init inside the microVM.

Accepts exactly one connection on the vsock agent port, runs one Magma job
with the limits the host sent, replies with the bounded result, and exits.
Init powers the guest off after this process returns, so nothing here is
reused by a later job.
"""
import ctypes
import errno
import os
import re
import resource
import signal
import socket
import subprocess
import sys
import threading

from firecracker import protocol
from firecracker.guest import seccomp_policy

REQUIRED = {"code": str, "env": dict, "magma_exe": str, "timeout": int, "cpu_timeout": int, "output_bytes": int}
STDERR_CAP = 64 * 1024
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_LOG_LINES = 20
SECCOMP_LOG_CHARS = 200


def seccomp_mode() -> str:
    """The guest's seccomp mode: AGENT_SECCOMP_MODE, else magma.seccomp= on the kernel command line."""
    override = os.environ.get("AGENT_SECCOMP_MODE")
    if override is not None:
        if override not in seccomp_policy.MODES:
            raise ValueError(f"unknown AGENT_SECCOMP_MODE: {override!r}")
        return override
    with open("/proc/cmdline", encoding="utf-8") as fh:
        return seccomp_policy.mode_from_cmdline(fh.read())


def _read_seccomp_log(path: str = "/dev/kmsg") -> list[str]:
    """Seccomp records (audit type 1326) from the kernel log, one per distinct syscall."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    try:
        while len(lines) < SECCOMP_LOG_LINES:
            try:
                record = os.read(fd, 8192)
            except OSError as exc:
                if exc.errno == errno.EPIPE:  # record overwritten while reading
                    continue
                break
            if not record:
                break
            text = record.decode("utf-8", errors="replace").partition(";")[2].strip()
            if "type=1326" not in text and "seccomp" not in text:
                continue
            key = re.search(r"syscall=\d+", text)
            key = key.group(0) if key else text
            if key in seen:
                continue
            seen.add(key)
            lines.append(text[:SECCOMP_LOG_CHARS])
    finally:
        os.close(fd)
    return lines


def _validate(request: dict) -> str | None:
    for key, typ in REQUIRED.items():
        if key not in request or not isinstance(request[key], typ):
            return f"invalid request: missing or wrong type for {key}"
    if request["timeout"] < 1 or request["cpu_timeout"] < 1 or request["output_bytes"] < 1:
        return "invalid request: limits must be positive"
    if request["output_bytes"] > protocol.MAX_REPLY_BYTES // 2:
        return "invalid request: output_bytes too large"
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in request["env"].items()):
        return "invalid request: env must map strings to strings"
    return None


def _drain(stream, cap: int, out: dict, key: str) -> None:
    """Read a pipe to EOF, keeping at most cap bytes. Never blocks the child."""
    kept = bytearray()
    truncated = False
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        if len(kept) < cap:
            room = cap - len(kept)
            kept += chunk[:room]
            if len(chunk) > room:
                truncated = True
        else:
            truncated = True
    out[key] = bytes(kept)
    out[key + "_truncated"] = truncated


def _failure(message: str, mode: str) -> dict:
    return {
        "stdout": "", "stderr": message, "exit_code": -1, "timed_out": False, "truncated": False,
        "seccomp_killed": False, "seccomp_mode": mode, "seccomp_log": [],
    }


def run_job(request: dict, run_as_uid: int | None = None, mode: str | None = None) -> dict:
    if mode is None:
        mode = seccomp_mode()
    error = _validate(request)
    if error:
        return _failure(error, mode)
    exe = request["magma_exe"]
    if not os.access(exe, os.X_OK):
        return _failure(f"magma.exe not executable: {exe}", mode)
    try:
        filt = seccomp_policy.build_filter(mode)
    except Exception as exc:  # noqa: BLE001 - fail closed: no filter, no job
        return _failure(f"cannot build seccomp filter: {exc}", mode)

    cpu = request["cpu_timeout"]
    libc = ctypes.CDLL(None, use_errno=True)

    def limits():
        if run_as_uid is not None:
            os.setgroups([])
            os.setgid(run_as_uid)
            os.setuid(run_as_uid)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        if filt is not None:
            filt.load()

    try:
        proc = subprocess.Popen(
            [exe, "-w", "-n"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=request["env"],
            preexec_fn=limits,
            cwd="/tmp",
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        where = "" if mode == "off" else f" (seccomp {mode})"
        return _failure(f"failed to start magma.exe{where}: {exc}", mode)

    captured: dict = {}
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, request["output_bytes"], captured, "stdout")),
        threading.Thread(target=_drain, args=(proc.stderr, STDERR_CAP, captured, "stderr")),
    ]
    for t in readers:
        t.start()

    def write_stdin():
        try:
            proc.stdin.write(request["code"].encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    stdin_thread = threading.Thread(target=write_stdin, daemon=True)
    stdin_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=request["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()
        proc.wait()

    stdin_thread.join()
    for t in readers:
        t.join()

    exit_code = proc.returncode
    seccomp_killed = False
    if exit_code < 0:
        exit_code = -1
        if timed_out or proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
            timed_out = True
        elif proc.returncode == -signal.SIGSYS:
            seccomp_killed = True

    return protocol.fit_reply({
        "stdout": captured["stdout"].decode("utf-8", errors="replace"),
        "stderr": captured["stderr"].decode("utf-8", errors="replace"),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": bool(captured["stdout_truncated"] or captured["stderr_truncated"]),
        "seccomp_killed": seccomp_killed,
        "seccomp_mode": mode,
        "seccomp_log": _read_seccomp_log() if mode == "log" else [],
    })


def serve_one(mode: str, port: int = protocol.AGENT_PORT, run_as_uid: int | None = None) -> None:
    listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    listener.bind((socket.VMADDR_CID_ANY, port))
    listener.listen(1)
    conn, _ = listener.accept()
    listener.close()
    try:
        try:
            request = protocol.recv_frame(conn, protocol.MAX_REQUEST_BYTES)
        except protocol.FrameError as exc:
            protocol.send_frame(conn, _failure(f"bad request frame: {exc}", mode))
            return
        protocol.send_frame(conn, run_job(request, run_as_uid=run_as_uid, mode=mode))
    finally:
        conn.close()


def main() -> int:
    uid = os.environ.get("AGENT_RUN_AS_UID")
    try:
        mode = seccomp_mode()
        print(f"agent: seccomp mode {mode}", file=sys.stderr, flush=True)
        serve_one(mode, run_as_uid=int(uid) if uid else None)
    except Exception as exc:  # noqa: BLE001 - one-shot process, report and exit
        print(f"agent: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
