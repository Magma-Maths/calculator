import asyncio
import json
import re
from pathlib import Path

from app.executor import SECCOMP_POLICY, execute_magma, wrap_magma_code, ExecutionResult
from app.config import Settings
from tests.fake_nsjail import jail_mounts

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "security" / "seccomp" / "magma.kafel"

# What magma.exe -w -n used under strace running the request wrapper, by
# strace's names.
MEASURED_SYSCALLS = """
access alarm arch_prctl brk clock_nanosleep close execve exit_group fstat getcwd
getpid getrandom ioctl lseek mmap mprotect munmap openat pread64 prlimit64
read rseq rt_sigaction rt_sigprocmask sched_getaffinity sched_setaffinity
set_robust_list set_tid_address socket stat times uname write
""".split()
# Kafel knows these by their kernel entry points.
KAFEL_NAME = {"stat": "newstat", "fstat": "newfstat", "uname": "newuname"}

DENIED_SYSCALLS = [
    "ptrace", "mount", "unshare", "setns", "bpf", "io_uring_setup", "keyctl",
    "connect", "sendto", "execveat", "process_vm_readv",
]


def test_wrap_magma_code():
    settings = Settings()
    wrapped = wrap_magma_code("print 1+1;", settings.magma_timeout)
    assert "Alarm(119);" in wrapped
    assert "SetIgnorePrompt(true);" in wrapped
    assert "print 1+1;" in wrapped
    assert wrapped.endswith(";\nquit;\n")


def test_wrap_magma_code_custom_timeout():
    wrapped = wrap_magma_code("x := 5;", 300)
    assert "Alarm(299);" in wrapped


def test_execution_result_dataclass():
    result = ExecutionResult(
        stdout="output",
        stderr="",
        exit_code=0,
    )
    assert result.stdout == "output"
    assert result.exit_code == 0


def test_jail_mounts_never_supply_executables_from_writable_space():
    """Configuration assertion only. nsjail cannot run in the test
    environment, so this checks the flags nsjail will hand to mount(2),
    not what the kernel then enforces.
    """
    mounts = {m["dst"]: m for m in jail_mounts()}

    tmp = mounts["/tmp"]
    assert tmp["fstype"] == "tmpfs"
    assert tmp["rw"] == "true"
    assert (tmp["noexec"], tmp["nosuid"], tmp["nodev"]) == ("true", "true", "true")

    writable = [dst for dst, m in mounts.items() if m.get("rw") == "true"]
    assert writable == ["/tmp"]
    for dst, m in mounts.items():
        assert (m.get("nosuid"), m.get("nodev")) == ("true", "true"), dst


def _policy_blocks(action: str) -> list[str]:
    """Bodies of the policy's `action { ... }` blocks, comments stripped."""
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", POLICY.read_text(), flags=re.S)
    bodies = []
    for m in re.finditer(rf"{re.escape(action)}\s*\{{", text):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        bodies.append(text[m.end():i - 1])
    return bodies


def _names_in(blocks: list[str]) -> set[str]:
    return {name for body in blocks for name in re.findall(r"\b[a-z_][a-z0-9_]*\b", body)}


def test_seccomp_policy_allows_measured_and_denies_escapes():
    """Text checks only. nsjail cannot run in the test environment, so this
    reads the policy kafel will compile, not the filter the kernel enforces.
    """
    allowed = _names_in(_policy_blocks("ALLOW"))
    missing = [s for s in MEASURED_SYSCALLS if KAFEL_NAME.get(s, s) not in allowed]
    assert missing == []
    assert sorted(allowed & set(DENIED_SYSCALLS)) == []
    assert "clone3" in _names_in(_policy_blocks("ERRNO(38)"))
    (allow,) = _policy_blocks("ALLOW")
    assert re.search(r"socket\(family, type, protocol\) \{[^}]*protocol == IPPROTO_IP", allow)
    assert "(option == PR_SET_PDEATHSIG && arg2 == SIGKILL)" in allow
    assert re.search(r"^#define SIGKILL 9$", POLICY.read_text(), re.M)
    assert POLICY.read_text().rstrip().splitlines()[-1] == "USE magma DEFAULT KILL_PROCESS"


def _nsjail_flags(record: Path) -> list[str]:
    return json.loads(record.read_text())


def test_seccomp_policy_passed_to_nsjail(jailed_magma, nsjail_launch):
    assert jailed_magma.post("/execute", json={"code": "print 1;"}).json()["exit_code"] == 0
    flags = _nsjail_flags(nsjail_launch)
    i = flags.index("--seccomp_policy")
    assert flags[i + 1] == SECCOMP_POLICY
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^WORKDIR /app$", dockerfile, re.M)
    assert re.search(r"^COPY security/ \./security/$", dockerfile, re.M)
    assert ROOT / Path(SECCOMP_POLICY).relative_to("/app") == POLICY


def test_seccomp_policy_off_with_jail_seccomp_false(jailed_magma, nsjail_launch, monkeypatch):
    monkeypatch.setenv("JAIL_SECCOMP", "false")
    from app import main
    monkeypatch.setattr(main, "settings", Settings())
    assert jailed_magma.post("/execute", json={"code": "print 1;"}).json()["exit_code"] == 0
    assert "--seccomp_policy" not in _nsjail_flags(nsjail_launch)


def test_seccomp_kill_reported(jailed_magma_killed_by_sigsys):
    from app import main
    result = asyncio.run(execute_magma("print 1;", main.settings))
    assert result.exit_code == -1
    assert result.stderr.startswith("killed by seccomp policy")


def test_seccomp_kill_not_classified_with_jail_seccomp_false(jailed_magma_killed_by_sigsys, monkeypatch):
    monkeypatch.setenv("JAIL_SECCOMP", "false")
    result = asyncio.run(execute_magma("print 1;", Settings()))
    assert result.exit_code == 159
    assert not result.stderr.startswith("killed by seccomp policy")


def test_job_cannot_fake_a_seccomp_kill(jailed_magma_faking_sigsys):
    from app import main
    result = asyncio.run(execute_magma("print 1;", main.settings))
    assert result.exit_code == 159
    assert not result.stderr.startswith("killed by seccomp policy")
