import errno
import tempfile

import pytest

from firecracker.guest import seccomp_policy as policy
from firecracker.host import jail

OBSERVED = (
    "access alarm arch_prctl brk clock_nanosleep close execve exit_group fstat getcwd getpid getrandom ioctl lseek "
    "mmap mprotect munmap openat pread64 prlimit64 read rseq rt_sigaction rt_sigprocmask sched_getaffinity "
    "sched_setaffinity set_robust_list set_tid_address socket stat times uname write"
).split()
MAC = "02:00:00:00:00:01"


def test_mode_defaults_to_on():
    assert policy.mode_from_cmdline("reboot=k panic=1 magma.mac=02:00:00:00:00:01") == "on"
    assert policy.mode_from_cmdline("") == "on"


@pytest.mark.parametrize("line", [
    "magma.seccomp=log reboot=k",
    "reboot=k magma.seccomp=log panic=1",
    "reboot=k panic=1 magma.seccomp=log",
])
def test_mode_found_anywhere_on_the_line(line):
    assert policy.mode_from_cmdline(line) == "log"


def test_mode_off_parsed():
    assert policy.mode_from_cmdline("magma.seccomp=off") == "off"


def test_mode_unknown_raises():
    with pytest.raises(ValueError):
        policy.mode_from_cmdline("x=1 magma.seccomp=strict")


def test_observed_syscalls_allowed():
    rule_names = {name for name, _ in policy.RULES}
    assert set(OBSERVED) <= policy.ALLOWED_SYSCALLS
    assert set(OBSERVED) <= rule_names


@pytest.mark.parametrize("name", [
    "ptrace", "mount", "unshare", "setns", "bpf", "io_uring_setup", "keyctl",
    "connect", "sendto", "execveat", "process_vm_readv",
])
def test_dangerous_syscalls_not_allowed(name):
    assert name not in policy.ALLOWED_SYSCALLS
    assert name not in {n for n, _ in policy.RULES}
    assert name not in policy.ERRNO_RULES


def test_conditional_syscalls_carry_arg_checks():
    by_name = {}
    for name, conds in policy.RULES:
        by_name.setdefault(name, []).append(conds)
    for name in ("socket", "ioctl", "prctl", "clone"):
        assert by_name[name] and all(by_name[name]), name
    assert by_name["socket"] == [[(0, "eq", 2, 0), (1, "masked_eq", 0xF, 2), (2, "eq", 0, 0)]]
    assert [(0, "eq", 1, 0), (1, "eq", 9, 0)] in by_name["prctl"]  # PR_SET_PDEATHSIG, SIGKILL only
    assert [(0, "eq", 1, 0)] not in by_name["prctl"]
    assert by_name["clone"] == [[(0, "masked_eq", policy.CLONE_NEW_MASK, 0)]]
    assert policy.CLONE_NEW_MASK & 0x10000000  # CLONE_NEWUSER
    assert [(1, "masked_eq", 0xFFFFFFFF, 0x8927)] in by_name["ioctl"]  # SIOCGIFHWADDR


def test_clone3_returns_enosys():
    assert policy.ERRNO_RULES == {"clone3": errno.ENOSYS}
    assert "clone3" not in policy.ALLOWED_SYSCALLS


def test_render_config_default_mode_on():
    args = jail.render_config(MAC, 512, 1)["boot-source"]["boot_args"]
    assert "magma.seccomp=on" in args
    assert "audit=1" not in args


def test_render_config_log_mode_enables_audit():
    args = jail.render_config(MAC, 512, 1, seccomp_mode="log")["boot-source"]["boot_args"]
    assert args.endswith("magma.seccomp=log audit=1")


def test_render_config_rejects_unknown_mode():
    with pytest.raises(ValueError):
        jail.render_config(MAC, 512, 1, seccomp_mode="strict")


def test_build_filter_off_is_none():
    assert policy.build_filter("off") is None


def test_build_filter_on_with_libseccomp():
    pytest.importorskip("seccomp")
    assert policy.build_filter("log") is not None
    with tempfile.TemporaryFile("w+") as fh:
        policy.build_filter("on").export_pfc(fh)
        fh.seek(0)
        pfc = fh.read()
    for name in ("socket", "ioctl", "clone3"):
        assert name in pfc
