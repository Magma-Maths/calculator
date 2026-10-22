# Firecracker worker

Each `/execute` job runs Magma in a disposable Firecracker microVM.
The API sends code to a host supervisor; Magma runs only inside the guest.
Follow [INSTALL.md](INSTALL.md) to set up your host, then use this guide
for the request flow, worker limits, and diagnostics.

## 1. What runs where

```text
+-----------------+   UNIX socket    +-------------------------+
|  API container  |----------------->|  magma-fc-supervisor    |
|  (unprivileged) | supervisor.sock  |  (root, one process)    |
+-----------------+                  +----------+--------------+
                                                | systemctl start
                                                | magma-fc@<slot>
                                                v
                                    +-------------------------+
                                    | magma-fc@<slot>.service |
                                    | runs magma-fc-launch    |
                                    +-----------+-------------+
                                                | exec jailer
                                                v
                                    +------------------------+
                                    | jailer -> firecracker  |
                                    | (chroot jail root)     |
                                    +-----------+------------+
                                                | boots guest (KVM)
                                                v
                                    +------------------------+
                                    | guest microVM          |
                                    | init.sh -> agent.py    |
                                    |         -> magma.exe   |
                                    +------------------------+

Job and result: supervisor <-> vsock port 52 <-> guest agent.py
```

- **API container:** Runs FastAPI as an unprivileged user. It never runs Magma. It submits jobs to the host supervisor through the UNIX socket `supervisor.sock` and receives results through that socket.

- **Supervisor:** `magma-fc-supervisor` runs as host root and owns a fixed pool of slots. Each free slot keeps a guest already booted and connected. The supervisor sends the job's code only when it hands the guest a job.

- **Slot service:** Each slot has one systemd unit, `magma-fc@<slot>.service`. The supervisor starts it with `systemctl`; the unit runs `magma-fc-launch` as host root to prepare and launch the microVM.

- **Jailer and Firecracker:** The launcher execs `jailer`, which chroots and drops to an unprivileged host uid/gid before execing the Firecracker VMM. Firecracker runs the guest through KVM.

- **Guest startup and storage:** The guest has no network device. `init.sh` runs as guest root, PID 1. It mounts the filesystems, runs the optional extension point `/etc/magma-fc/site-init`, and starts `agent.py`. Both disks are attached read-only: `rootfs.ext4` (`/dev/vda`) supplies `/`; `magma.ext4` (`/dev/vdb`) is mounted read-only at `/opt/magma/current`. Writable storage is tmpfs: `/tmp` (64 MiB) and `/run` (8 MiB), both mounted `noexec,nosuid,nodev`.

- **Agent and Magma:** `agent.py` runs as guest root and accepts exactly one job over vsock port 52. It drops the Magma child to guest uid/gid 1000, applies resource limits and a seccomp filter, and execs `/opt/magma/current/magma.exe -w -n`. Magma runs inside the guest with working directory `/tmp` and the submitted code on stdin.

- **Result and replacement:** The agent returns bounded output over vsock to the supervisor, which returns it to the API. The agent exits and the guest reboots. The supervisor destroys the slot's unit and jail, then boots a replacement guest.

The API has no KVM access. The jailer enters a new PID namespace. Requests
cannot choose a host path, command, image, or unit name; the supervisor
takes those from its configuration.

The supervisor validates requests and prefers a ready guest when claiming
a slot. At handoff, it adds the Magma environment and executable path.
Background boots receive no job code or environment. The API wraps code with
an alarm, prompt settings, and `quit`, then sends it with numeric limits.
The agent returns bounded output and job metrics. The supervisor bounds the
reply again and removes the jail after its cgroup empties, then returns the
reply to the API. The API formats the HTTP response while the replacement
boots. Guest root is separate from host root.

### Warm pool

Each free slot keeps its next guest booted and connected to the agent.
Idle guests hold their configured memory. After a job, the supervisor
releases the slot and boots its replacement in the background.

If no guest is ready, a job claims a booting slot and waits for it.
If that boot fails, or its guest is unusable when the wait ends, the job
fails. A guest already dead or too old at handoff is released and replaced
with one fresh boot before the code is sent.
A failed background boot is not retried in the background. The next job
on that slot waits for cleanup and boots on demand.

Cleanup and boot add time before handoff. A failed background boot may
already be cleaning up when claimed; the job then performs another release
before booting on demand. Each release includes the unit's `TimeoutStopSec`
and an 8-second cgroup drain. The guest exchange allows `timeout + 5`
seconds, and the job's final release completes before the reply.
The API read deadline is `MAGMA_TIMEOUT + 60` seconds.

Idle time counts toward the unit's lifetime. The supervisor replaces idle
guests after `max(runtime_max_sec - max_timeout - 5, boot_timeout)` seconds
from the unit-start attempt. The INSTALL example gives 25 seconds,
including boot time.
At handoff it also checks for a closed connection and enough remaining
unit lifetime for the requested timeout plus five seconds.
Stopping the supervisor waits for outstanding staging and cleanup, then
tears down idle guests.

## 2. Host layout

These paths match the setup in INSTALL.md:

| Path | Purpose |
| --- | --- |
| `/opt/magma-fc/v1.17.0/{firecracker,jailer}` | Firecracker release binaries. |
| `/opt/magma-fc/src` | Checkout; supervisor and `fcrun` use it as `PYTHONPATH`. |
| `/opt/magma-fc/bin/magma-fc-launch` | Launcher run by each slot unit. |
| `/srv/magma-fc/images/{vmlinux,rootfs.ext4,magma.ext4}` | Kernel and disk images, mode 0640, owner root:firecracker. |
| `/srv/magma-fc/jails/firecracker/<slot>/root` | Slot's staged jail, removed after the unit drains. |
| `/etc/magma-fc/{supervisor.json,worker.env}` | Supervisor configuration and slot-unit environment. |
| `/run/magma-fc/supervisor.sock` | API socket, mode 0660, owner root:magma-api. |

Images and jails must share a filesystem because staging uses hard links.
The images retain their original ownership. Only the slot directory,
its `root`, `run`, and `config.json` are owned by the jailed UID/GID.
Keep shared images and their parent directories writable only by root.

## 3. The protocols

The API can send `{"operation":"metadata"}` over the supervisor socket.
This read-only operation returns only `guest_memory_bytes`,
`max_timeout_seconds`, and `max_output_bytes`. It claims no execution slot,
boots no guest, and remains available when all job slots are occupied.
It shares the socket's connection cap and I/O deadlines.
The API sends at most one at a time and reuses each reply for 10 seconds.
`/info` treats a missing, invalid, or unavailable reply as unconfirmed limits.

The API opens one UNIX-socket connection per job. Its request contains
`code` (string), `timeout`, `cpu_timeout`, and `output_bytes` (integers).
The supervisor adds `env` (string-to-string mapping) and `magma_exe`
(string) for the guest request. It constructs the Magma environment from
`app/magma_cmd.py`, with `PATH=/usr/bin:/bin`, `HOME=/tmp`, and `TMPDIR=/tmp`.

After sending the request, the API keeps its write side open until the job
finishes. EOF cancels the job, including when the HTTP client disconnects.
The API half-closes the socket to request cancellation and keeps reading
the reply. The supervisor stops and drains the guest before replying with
`error: "cancelled"`; teardown finishes even if cancellation arrives while
the guest is already stopping. API capacity remains occupied until that
reply or a transport failure. Older supervisors finish the job normally
before replying, so deploy matching API and supervisor versions together.

A successful worker exchange returns these fields on both hops:

| Fields | Meaning |
| --- | --- |
| `stdout`, `stderr` | Captured output. |
| `exit_code`, `timed_out`, `truncated` | Process result and limit flags. |
| `seccomp_killed`, `seccomp_mode`, `seccomp_log` | Filter result, mode, and bounded log records. |
| `cpu_time_sec`, `peak_memory_kb` | Child resource usage, or `null` when unavailable. |

The agent measures CPU time and peak memory with `getrusage`, independently
of stdout. The supervisor bounds those values before forwarding them;
the API checks them again.

Supervisor failures have this shape:

```json
{"error":"worker_failed","stdout":"","stderr":"guest did not come up","exit_code":-1,"timed_out":false,"truncated":false}
```

`busy` means no slot is free or the connection cap was reached.
`bad_request` means validation failed, including size and limit checks.
`worker_failed` covers boot failures, lost guest connections, bad replies,
and internal errors. A missed guest reply deadline sets `timed_out: true`.

### Vsock connection

Firecracker exposes the guest listener through `/run/vsock.sock` inside
its jail. The host connects to that UNIX socket, sends `CONNECT 52\n`,
and waits for `OK <n>\n`.
It retries until `boot_timeout` expires. It rejects a symlink at the socket
path and checks `SO_PEERCRED`: the peer UID must match the staged jail UID.

### Frames

Both protocols use a four-byte big-endian length followed by UTF-8 JSON,
without a newline. The reader checks the length before reading the body.
The shared constants in `firecracker/protocol.py` are:

- `AGENT_PORT = 52`.
- `MAX_REQUEST_BYTES = 256 KiB`, on both request hops.
- `MAX_REPLY_BYTES = 1 MiB`, on both reply hops.

The code-size check reserves 16 KiB for the environment and limits added
by the supervisor. Limits count encoded JSON bytes. A control character
can take six bytes, so a request can exceed the frame budget below the
API's raw input limit. Oversized replies are trimmed and marked truncated.

## 4. Building images by hand

```bash
firecracker/build/build-kernel.sh /srv/magma-fc/images/vmlinux
firecracker/build/build-rootfs.sh /opt/magma-fc/src /srv/magma-fc/images/rootfs.ext4
firecracker/build/build-magma-image.sh /path/to/magma-version /srv/magma-fc/images/magma.ext4
chown root:firecracker /srv/magma-fc/images/{vmlinux,rootfs.ext4,magma.ext4}
chmod 0640 /srv/magma-fc/images/{vmlinux,rootfs.ext4,magma.ext4}
```

The three builders need host Python 3.11 or later.
Each successful run prints `{"changed":true}` or `{"changed":false}` on stdout.
Restart services only after a successful run reports `changed:true`.
A failed build exits nonzero and keeps the previous image.

Pass `--force` before the paths after toolchain updates, or after editing an
installed Magma tree in place. Also use it for new guest packages from the
Debian repository, or a change published under the same remote version or tag.
Keep each image's `.build-stamp.json` and `.build.lock` files beside it.

`build-kernel.sh` fetches Firecracker's CI kernel configuration and builds
`vmlinux`. It needs a kernel build toolchain. Defaults are Linux 6.1.186
and Firecracker v1.17.0; `KERNEL_VERSION` and `FIRECRACKER_TAG` override them.
The second argument is a build directory, default `/var/tmp/magma-fc-kernel`.
An optional third argument supplies an extra kernel configuration fragment.
The build fails if a requested setting is absent from the resolved config.

`build-rootfs.sh` builds a Debian bookworm minbase image containing init,
the agent, and its seccomp policy. It needs root, `mmdebstrap`, and
`e2fsprogs`. An optional third argument installs an executable site hook
at `/etc/magma-fc/site-init`. Init runs it as guest root, without arguments,
after filesystem mounts and before the agent. A failed hook stops boot.

`build-magma-image.sh` packs a version directory containing executable
`magma.exe` into an ext4 image. It needs `e2fsprogs`. The image is attached
read-only as `/dev/vdb` and mounted at `/opt/magma/current`.

Each slot in `supervisor.json` has a `name` and optional `boot_args` string.
This extension is appended to the kernel command line and defaults to empty.
It accepts up to 1024 ASCII characters as unquoted `key=value` tokens,
separated by single spaces. Keys accept letters, digits, `_`, `.`, and `-`;
values also accept `/,:+@%=`.

The scripts do not set the required final ownership. The kernel is written
with mode 0644; the two disk images use 0640. Apply the ownership and mode
commands above before starting the supervisor.

## 5. First boot by hand

Run the debug launcher as root: it uses `/dev/kvm` directly.

```bash
firecracker/host/fc-debug-boot.sh \
  /srv/magma-fc/images/vmlinux /srv/magma-fc/images/rootfs.ext4 \
  /srv/magma-fc/images/magma.ext4 'site.slot=one'
```

The fourth argument accepts the same optional boot-argument string as the
supervisor. This launcher uses a serial console, with no jailer or vsock
device. Use it to inspect early boot; run jobs through the supervisor.
The optional fifth argument sets guest RAM in MiB, default 1024.
`FIRECRACKER` overrides the default binary at
`/opt/magma-fc/v1.17.0/firecracker`.

To open a root shell before init runs:

```bash
BOOT_ARGS=init=/bin/bash firecracker/host/fc-debug-boot.sh \
  /srv/magma-fc/images/vmlinux /srv/magma-fc/images/rootfs.ext4 \
  /srv/magma-fc/images/magma.ext4 'site.slot=one'
```

Run `/opt/calculator-guest/init.sh` to perform the normal guest setup.
The agent cannot receive a job in this debug guest because it has no vsock
device.

## 6. Smoke test through the supervisor

Run `fcrun` as a user in the socket's `magma-api` group:

```bash
PYTHONPATH=/opt/magma-fc/src \
  python3 -m firecracker.host.fcrun -e 'print 1+1;'
```

INSTALL.md creates the group; a separate host user is optional.
A user outside that group gets a connection error.
`fcrun` defaults to `/run/magma-fc/supervisor.sock`, a 60-second timeout,
and a 20 KiB output cap. Use `--socket`, `--timeout`, or `--output-bytes`
to override them, and `--code-file` for longer snippets.
It prints the full JSON reply and exits 2 if it has an `error` field,
0 otherwise.

Start with the default `guest_seccomp: "on"`. If the reply contains
`seccomp_killed: true`, use [log mode](#seccomp) to identify the syscall
and update the policy before returning to `on`.

## 7. Checks after a job

```bash
systemctl status magma-fc@slot1
ls /srv/magma-fc/jails/firecracker/
journalctl -u magma-fc-supervisor
journalctl -u magma-fc@slot1
```

The production guest's serial console is disabled. The slot journal carries
Firecracker and jailer startup errors, such as a bad configuration, missing
image, or chroot failure. Guest output returns through vsock.

After a clean job, the supervisor stops the unit and removes its jail once
the cgroup drains. The replacement may already be booting when you inspect
the slot, so a running unit or present jail can belong to the next guest.

A failed stop, drain, or cleanup quarantines the slot. It stays unavailable
until supervisor restart; startup checks every configured slot again.
Quarantine is supervisor process state, not a systemd or filesystem flag.
A failed stop or drain leaves the jail for inspection; cleanup failure may
leave only part of it. Look for `quarantining` in the supervisor journal.

The supervisor logs unit start failures with `systemctl` output, vsock
connection failures, exchange errors, missed reply deadlines, and bad reply
frames. Unexpected job exceptions log `job failed on <slot>` with a
traceback; exceptions outside the slot scope log `unhandled error running job`.
Request validation adds no separate error log.

Every `run_job()` call logs the slot, outcome, and free/quarantined counts,
including `busy` and `bad_request` calls, where the slot is `-`.
Startup logs the counts after resetting the configured slots.
Idle quarantine logs `idle <slot> quarantined (N free, N quarantined)`.

## 8. Limits and where they are set

| Limit | Setting and enforcement |
| --- | --- |
| Guest RAM | `mem_mib` in `supervisor.json`, copied to `machine-config.mem_size_mib`. |
| Virtual CPUs | `vcpus` in `supervisor.json`, copied to `vcpu_count`; SMT is disabled. |
| Unit resources | Shipped unit: `MemoryMax=1536M`, `MemorySwapMax=0`, `CPUQuota=100%`, `TasksMax=64`, and `OOMPolicy=kill`. |
| Unit lifetime | `RuntimeMaxSec=150`, counting boot, idle time, and execution. |
| Stop grace | `TimeoutStopSec=5`, then supervisor cgroup drain for up to 8 seconds. |
| Per-job limits | Request `timeout`, `cpu_timeout`, and `output_bytes`, from API settings `MAGMA_TIMEOUT`, `MAGMA_CPU_TIMEOUT`, and `MAGMA_OUTPUT_KB`. |
| Supervisor timeout ceiling | `max_timeout`, default 300; larger wall or CPU timeout requests are rejected as `bad_request` with `timeout too large`. |
| API connections | `max_connections`, default `2 * slots + 2`, counting request reads, jobs, and reply writes. Extra connections receive `busy`. |

The unit memory limit covers the jailed process tree, including the VMM's
allocation for guest RAM. It is separate from the guest's configured RAM.
The agent sets `RLIMIT_CPU` to `(cpu_timeout, cpu_timeout + 1)` and kills
the child's process group when wall time expires. It also disables core
dumps. The supervisor allows `timeout + 5` seconds for the guest exchange.

The agent caps stdout at `output_bytes` and stderr at 64 KiB, draining both
pipes even after those caps. The supervisor applies the same caps again
before forwarding the reply. An `output_bytes` request above half the
reply frame budget is rejected. Encoded replies must also fit the
[frame budget](#frames).

The supervisor allows ten seconds to read an API request and ten seconds
to write its reply. A stalled connection is closed.

Keep `max_timeout + boot_timeout + 5 < runtime_max_sec`, and make
`runtime_max_sec` match the unit's `RuntimeMaxSec`. Otherwise systemd can
stop a guest while its job remains within the requested limits.
Startup logs a warning with those values if the inequality fails; it
still starts. Changing the JSON setting does not change systemd's limit.

## 9. Security notes

The guest VM isolates each job. Each guest runs one job, and its jail is
removed after the host confirms that the unit's cgroup is empty.
The jailed VMM runs under an unprivileged host UID/GID. The unit uses
`NoNewPrivileges=yes`, a private host network namespace,
`RestrictAddressFamilies=AF_UNIX`, and `IPAddressDeny=any`.

The guest has no network device, so it has no route to the host, to other
slots or to a cloud metadata server.

### Accepted residual risk

A job that gains root inside its own guest can read the Magma image
attached to that guest. Read-only disks protect image integrity, not
secrecy, and any process can read the binary it is running. Root-owned
shared images also prevent the jailed host UID from rewriting them. The
seccomp filter adds protection inside the disposable guest; the VM remains
the job isolation boundary.

### Seccomp

The policy is in `firecracker/guest/seccomp_policy.py`.
The agent clears supplementary groups, drops the Magma child to guest
UID/GID 1000, sets `PR_SET_NO_NEW_PRIVS`, and loads the filter before exec.
A filter construction or load failure prevents the job from starting.

`guest_seccomp` selects the mode. The supervisor passes it as the kernel
argument `magma.seccomp=`:

- `on` (default): disallowed syscalls kill Magma with SIGSYS.
- `log`: disallowed syscalls are allowed and logged. The supervisor adds
  `audit=1` to the kernel command line.
- `off`: no filter.

The policy returns `ENOSYS` for `clone3` in both filtered modes, allowing
callers to fall back to the filtered `clone` syscall.

`seccomp_killed: true` means Magma received SIGSYS; the exit code is `-1`.
To investigate, set `guest_seccomp: "log"`, restart the supervisor, rerun
the job, and read `seccomp_log` in the reply. The log contains at most
20 distinct syscall records, each capped at 200 characters.
Update the policy and restore `on`. The debug launcher cannot run this
check because its guest has no vsock device to receive a job.
