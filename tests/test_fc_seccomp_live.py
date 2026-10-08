"""Load the guest policy's RULES into a real kernel filter and run under it.

The guest builds the filter with python3-seccomp; the host usually has only
libseccomp.so.2, so this drives the C library through ctypes.
"""
import ctypes
import ctypes.util
import os
import signal
import socket
import subprocess

import pytest

from app.magma_cmd import wrap_magma_code
from firecracker.guest import seccomp_policy as policy
from firecracker.host.supervisor import guest_environment

MAGMA_ROOT = "/opt/magma/current"
MAGMA_EXE = f"{MAGMA_ROOT}/magma.exe"
LIBSECCOMP = ctypes.util.find_library("seccomp")

pytestmark = pytest.mark.skipif(
    not (os.access(MAGMA_EXE, os.X_OK) and LIBSECCOMP),
    reason="needs /opt/magma/current/magma.exe and libseccomp.so.2",
)

ACT_KILL_PROCESS = 0x80000000
ACT_ALLOW = 0x7FFF0000
ACT_ERRNO = 0x00050000
ATTR_ACT_BADARCH = 2
OPS = {"eq": 4, "masked_eq": 7}


class ArgCmp(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def load_policy(rules=policy.RULES, errno_rules=policy.ERRNO_RULES):
    """Install rules in the calling process in "on" mode; runs post-fork."""
    lib = ctypes.CDLL(LIBSECCOMP)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgCmp)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]

    ctx = lib.seccomp_init(ACT_KILL_PROCESS)
    if not ctx or lib.seccomp_attr_set(ctx, ATTR_ACT_BADARCH, ACT_KILL_PROCESS):
        raise OSError("seccomp_init failed")

    def add(action, name, conds):
        nr = lib.seccomp_syscall_resolve_name(name.encode())
        args = (ArgCmp * max(len(conds), 1))(*[ArgCmp(i, OPS[op], a, b) for i, op, a, b in conds])
        rc = lib.seccomp_rule_add_array(ctx, action, nr, len(conds), args)
        if rc:
            raise OSError(-rc, f"seccomp_rule_add_array({name})")

    for name, conds in rules:
        add(ACT_ALLOW, name, conds)
    for name, err in errno_rules.items():
        add(ACT_ERRNO | err, name, [])
    rc = lib.seccomp_load(ctx)
    if rc:
        raise OSError(-rc, "seccomp_load")


def test_wrapped_job_runs_under_the_policy():
    proc = subprocess.run(
        [MAGMA_EXE, "-w", "-n"],
        input=wrap_magma_code("print 2+2;", 10).encode(),
        capture_output=True, env=guest_environment(MAGMA_ROOT), cwd="/tmp",
        preexec_fn=load_policy, timeout=30,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert b"4" in proc.stdout.split()


def _socket_in_filtered_child(family, kind, proto):
    pid = os.fork()
    if pid == 0:
        try:
            load_policy()
            libc = ctypes.CDLL(None)
            libc.socket(family, kind, proto)
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    return status


@pytest.mark.parametrize("family, kind, proto, killed", [
    (socket.AF_INET, socket.SOCK_DGRAM, 0, False),
    (socket.AF_UNIX, socket.SOCK_STREAM, 0, True),
    (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, True),
])
def test_socket_rule(family, kind, proto, killed):
    status = _socket_in_filtered_child(family, kind, proto)
    if killed:
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSYS
    else:
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
