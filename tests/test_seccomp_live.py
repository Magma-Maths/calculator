"""The seccomp policy enforced by the kernel on the real magma.exe.

nsjail and kafel are not available here, so the test rebuilds the policy
with libseccomp from security/seccomp/magma.kafel: plain ALLOW names are
read from the file, and the four argument rules are translated below with
the file's own #define values. A launcher loads that filter into itself
and then runs the given action, as nsjail loads it just before execve.
"""
import ctypes.util
import json
import os
import platform
import re
import signal
import subprocess
import sys

import pytest

from app.config import Settings
from app.executor import magma_environment, wrap_magma_code
from tests.test_executor import POLICY, _policy_blocks

MAGMA_ROOT = Settings().magma_root
MAGMA_EXE = os.path.join(MAGMA_ROOT, "magma.exe")

pytestmark = pytest.mark.skipif(
    platform.machine() != "x86_64"
    or not os.path.exists(MAGMA_EXE)
    or ctypes.util.find_library("seccomp") is None,
    reason="needs x86_64, libseccomp.so.2 and a Magma install",
)

# Kafel's names for the kernel entry points libseccomp calls by their
# usual names.
LIBSECCOMP_NAME = {"newstat": "stat", "newfstat": "fstat", "newlstat": "lstat", "newuname": "uname"}
CONDITIONED = {"socket", "ioctl", "prctl", "clone"}

EQ, MASKED_EQ = 4, 7  # enum scmp_compare


def _defines() -> dict[str, int]:
    return {
        name: int(value, 0)
        for name, value in re.findall(r"^#define (\w+) (\S+)", POLICY.read_text(), re.M)
    }


def _allow_entries() -> tuple[list[str], list[str]]:
    """Plain names and names with an argument rule in the ALLOW block."""
    (body,) = _policy_blocks("ALLOW")
    entries, depth, start = [], 0, 0
    for i, ch in enumerate(body + ","):
        depth += {"{": 1, "(": 1, "}": -1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            entries.append(body[start:i].strip())
            start = i + 1
    plain = [e for e in entries if re.fullmatch(r"\w+", e)]
    conditioned = [re.match(r"\w+", e).group() for e in entries if e and e not in plain]
    return plain, conditioned


def _filter_spec() -> dict:
    """Rules as [name, [[arg, op, datum_a, datum_b], ...]], one rule per OR branch."""
    d = _defines()
    plain, conditioned = _allow_entries()
    assert set(conditioned) == CONDITIONED, "translate the new argument rule below"
    low32 = d["LOW32"]
    ioctls = ["TCGETS2", "TCGETS", "TIOCGWINSZ", "SIOCGIFHWADDR", "SIOCGIFCONF",
              "SIOCGIFFLAGS", "FIONREAD", "FIOCLEX", "FIONBIO"]
    prctls = ["PR_SET_NAME", "PR_GET_NAME", "PR_SET_DUMPABLE", "PR_GET_DUMPABLE",
              "PR_SET_NO_NEW_PRIVS"]
    allow = [[LIBSECCOMP_NAME.get(n, n), []] for n in plain]
    allow.append(["socket", [[0, EQ, d["AF_INET"], 0],
                             [1, MASKED_EQ, d["SOCK_TYPE_MASK"], d["SOCK_DGRAM"]],
                             [2, EQ, d["IPPROTO_IP"], 0]]])
    allow += [["ioctl", [[1, MASKED_EQ, low32, d[c]]]] for c in ioctls]
    allow += [["prctl", [[0, EQ, d[o], 0]]] for o in prctls]
    allow.append(["prctl", [[0, EQ, d["PR_SET_PDEATHSIG"], 0], [1, EQ, d["SIGKILL"], 0]]])
    allow.append(["clone", [[0, MASKED_EQ, d["CLONE_NEW_MASK"], 0]]])
    return {"allow": allow, "enosys": ["clone3"]}


# Loads the filter into this process, then execs argv or opens an AF_UNIX
# socket. socket is imported first: nothing after the load may need more
# than the policy allows.
LAUNCHER = r"""
import ctypes, json, os, socket, sys

class Cmp(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]

lib = ctypes.CDLL("libseccomp.so.2")
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                       ctypes.c_uint, ctypes.POINTER(Cmp)]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]

spec = json.loads(sys.argv[1])
ctx = lib.seccomp_init(0x80000000)  # SCMP_ACT_KILL_PROCESS

def add(action, name, cmps):
    nr = lib.seccomp_syscall_resolve_name(name.encode())
    assert nr >= 0, name
    arr = (Cmp * max(len(cmps), 1))(*[Cmp(*c) for c in cmps])
    assert lib.seccomp_rule_add_array(ctx, action, nr, len(cmps), arr) == 0, name

for name, cmps in spec["allow"]:
    add(0x7FFF0000, name, cmps)  # SCMP_ACT_ALLOW
for name in spec["enosys"]:
    add(0x00050000 | 38, name, [])  # SCMP_ACT_ERRNO(ENOSYS)
assert lib.seccomp_load(ctx) == 0

if sys.argv[2] == "unix-socket":
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sys.exit(0)
os.execve(sys.argv[2], sys.argv[2:], os.environ)
"""


def _run_filtered(action: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    root = os.path.realpath(MAGMA_ROOT)
    env = dict(e.split("=", 1) for e in magma_environment(root))
    return subprocess.run(
        [sys.executable, "-c", LAUNCHER, json.dumps(_filter_spec()), *action],
        input=stdin, capture_output=True, text=True, env=env, timeout=10,
    )


def test_real_magma_runs_the_request_wrapper_under_the_policy():
    root = os.path.realpath(MAGMA_ROOT)
    program = wrap_magma_code("print 2+2;", Settings().magma_timeout)
    result = _run_filtered([f"{root}/magma.exe", "-w", "-n"], program)
    assert result.returncode == 0, result.stderr
    assert "4" in result.stdout.split()


def test_unix_socket_is_killed_by_sigsys():
    result = _run_filtered(["unix-socket"])
    assert result.returncode == -signal.SIGSYS
