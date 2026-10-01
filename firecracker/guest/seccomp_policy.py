"""Seccomp allow-list for the Magma child inside the guest.

The rules are plain data so the host can test them without libseccomp;
build_filter() turns them into a libseccomp filter inside the guest.
"""
import errno
import re

MODES = ("on", "log", "off")

OBSERVED = frozenset("""
access arch_prctl brk clock_nanosleep close execve exit_group fstat getcwd
getpid getrandom ioctl lseek mmap mprotect munmap openat pread64 prlimit64
read rseq rt_sigaction rt_sigprocmask sched_getaffinity sched_setaffinity
set_robust_list set_tid_address socket stat times uname write
""".split())

# close_range: Python's subprocess child closes inherited fds with it after
# preexec_fn has loaded the filter, before it execs magma.exe.
BASELINE = frozenset("""
clone futex madvise mremap exit getdents64 newfstatat statx readlink
readlinkat fcntl dup dup2 dup3 pipe pipe2 poll ppoll select pselect6
epoll_create1 epoll_ctl epoll_wait wait4 waitid kill tgkill tkill sigaltstack
gettid getuid geteuid getgid getegid getppid getpgrp sysinfo getrusage
nanosleep clock_gettime clock_getres gettimeofday sched_yield rt_sigreturn
rt_sigsuspend rt_sigpending rt_sigtimedwait writev readv pwrite64 preadv
pwritev unlink unlinkat mkdir mkdirat rmdir rename renameat renameat2
ftruncate truncate fsync fdatasync fadvise64 flock lstat fstatfs statfs
faccessat faccessat2 chdir fchdir umask utimensat memfd_create mlock munlock
getpriority setpriority sched_getparam sched_getscheduler get_mempolicy
restart_syscall prctl close_range
""".split())

ALLOWED_SYSCALLS = OBSERVED | BASELINE

AF_INET = 2
SOCK_DGRAM = 2
SOCK_TYPE_MASK = 0xF
CLONE_NEW_MASK = 0x20000 | 0x4000000 | 0x8000000 | 0x10000000 | 0x20000000 | 0x40000000 | 0x2000000 | 0x80
PRCTL_ALLOWED = (
    15,  # PR_SET_NAME
    16,  # PR_GET_NAME
    4,   # PR_SET_DUMPABLE
    3,   # PR_GET_DUMPABLE
    38,  # PR_SET_NO_NEW_PRIVS
    1,   # PR_SET_PDEATHSIG
)
IOCTL_ALLOWED = (
    0x802C542A,  # TCGETS2
    0x5401,      # TCGETS
    0x5413,      # TIOCGWINSZ
    0x8927,      # SIOCGIFHWADDR
    0x8912,      # SIOCGIFCONF
    0x8913,      # SIOCGIFFLAGS
    0x541B,      # FIONREAD
    0x5451,      # FIOCLEX
    0x5421,      # FIONBIO
)
# The kernel reads the ioctl request as a 32-bit int, so compare only the
# low 32 bits: a caller that sign-extends TCGETS2 must still match.
LOW32 = 0xFFFFFFFF

# A condition is (arg_index, op, datum_a, datum_b); op "eq" compares arg to
# datum_a, op "masked_eq" checks (arg & datum_a) == datum_b. Several rules
# for one syscall are ORed; the conditions inside one rule are ANDed.
_CONDITIONAL = {
    "socket": [[(0, "eq", AF_INET, 0), (1, "masked_eq", SOCK_TYPE_MASK, SOCK_DGRAM)]],
    "ioctl": [[(1, "masked_eq", LOW32, cmd)] for cmd in IOCTL_ALLOWED],
    "prctl": [[(0, "eq", option, 0)] for option in PRCTL_ALLOWED],
    "clone": [[(0, "masked_eq", CLONE_NEW_MASK, 0)]],
}

RULES = [
    (name, conds)
    for name in sorted(ALLOWED_SYSCALLS)
    for conds in _CONDITIONAL.get(name, [[]])
]

# clone3 passes its flags in a struct that seccomp cannot inspect, so it
# cannot get clone's CLONE_NEW* check. glibc retries with clone on ENOSYS.
ERRNO_RULES = {"clone3": errno.ENOSYS}

_CMDLINE_RE = re.compile(r"(?:^|\s)magma\.seccomp=(\S*)")


def mode_from_cmdline(cmdline: str) -> str:
    match = _CMDLINE_RE.search(cmdline)
    if not match:
        return "on"
    mode = match.group(1)
    if mode not in MODES:
        raise ValueError(f"unknown magma.seccomp mode: {mode!r}")
    return mode


def build_filter(mode: str):
    """Build (but do not load) the filter for mode; None for "off"."""
    if mode not in MODES:
        raise ValueError(f"unknown seccomp mode: {mode!r}")
    if mode == "off":
        return None
    import seccomp

    ops = {"eq": seccomp.EQ, "masked_eq": seccomp.MASKED_EQ}
    default = seccomp.KILL_PROCESS if mode == "on" else seccomp.LOG
    filt = seccomp.SyscallFilter(defaction=default)
    # libseccomp kills only the calling thread on a non-native (i386, x32)
    # syscall unless told otherwise, and would kill even in "log" mode.
    filt.set_attr(seccomp.Attr.ACT_BADARCH, default)
    for name, conds in RULES:
        args = [seccomp.Arg(i, ops[op], a, b) for i, op, a, b in conds]
        filt.add_rule(seccomp.ALLOW, name, *args)
    for name, err in ERRNO_RULES.items():
        filt.add_rule(seccomp.ERRNO(err), name)
    return filt
