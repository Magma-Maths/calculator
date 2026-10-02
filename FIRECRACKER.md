# Firecracker worker

Operator notes for the Firecracker backend: an alternative to the in-container
nsjail sandbox that runs each `/execute` job in its own cold-booted microVM.
Code lives under `firecracker/`; the API opts in with `EXECUTOR_BACKEND=firecracker`
(see `calculator.env.example`).

## 1. What runs where

```
+-----------------+   UNIX socket    +------------------------+
|  API container  |----------------->|  magma-fc-supervisor    |
|  (unprivileged) | supervisor.sock  |  (root, one process)    |
+-----------------+                  +-----------+------------+
                                                  | systemctl start magma-fc@<slot>
                                                  v
                                      +------------------------+
                                      |  magma-fc@<slot>.service|
                                      |  runs magma-fc-launch   |
                                      +-----------+------------+
                                                  | exec jailer
                                                  v
                                      +------------------------+
                                      |  jailer -> firecracker  |
                                      |  (chroot jail root)     |
                                      +-----------+------------+
                                                  | vsock CONNECT 52
                                                  v
                                      +------------------------+
                                      |  guest microVM           |
                                      |  init.sh -> agent.py     |
                                      +------------------------+
```

The API container never sees Magma, the passfile, or any virtualization
authority; it only ever talks to the supervisor socket. The supervisor owns a
fixed pool of named slots, one systemd unit per slot. Each unit execs the
jailer, which chroots and execs Firecracker. Inside the guest, init mounts the
Magma image, creates the licence interface lic0, administratively down, with
the slot's MAC, and runs the agent, which handles exactly one job and powers
the guest off.

With `EXECUTOR_BACKEND=firecracker`, the API container runs as the image's
`calculator` user, since it no longer needs root for nsjail's namespace
setup; under the nsjail backend it still runs as root.

## 2. Host layout

- `/opt/magma-fc/v1.17.0/{firecracker,jailer}` - the pinned Firecracker release binaries
- `/opt/magma-fc/src` - calculator checkout; `PYTHONPATH` for the supervisor and `fcrun`
- `/opt/magma-fc/bin/magma-fc-launch` - exec'd by the per-slot unit
- `/srv/magma-fc/images/{vmlinux,rootfs.ext4,magma-<version>.ext4}` - built images, read-only, root:firecracker
- `/srv/magma-fc/jails/firecracker/<slot>/root` - per-job jail directory staged and torn down by the supervisor
- `/etc/magma-fc/{supervisor.json,worker.env}` - supervisor config and the unit's environment file
- `/run/magma-fc/supervisor.sock` - the API-facing socket, mode 0660, root:magma-api

## 3. The protocols

**API to supervisor** (over `/run/magma-fc/supervisor.sock`, one connection per job):
request `{"code": str, "timeout": int, "cpu_timeout": int, "output_bytes": int}`;
reply `{"stdout", "stderr", "exit_code", "timed_out", "truncated"}`, or on
failure `{"error": "busy"|"worker_failed"|"bad_request", "stdout": "", "stderr": str, "exit_code": -1, "timed_out": bool, "truncated": false}`.
`busy` means no free slot, `bad_request` means the request failed validation
(limits, size), `worker_failed` covers everything else (boot timeout, guest
crash, bad reply).

**Supervisor to guest** (over vsock, one connection per job): request adds
`env` (the full Magma environment) and `magma_exe` to the four fields above;
reply is the same five-field object, produced directly by the agent.

**vsock CONNECT handshake**: Firecracker exposes the guest's `AF_VSOCK`
listeners through one UNIX socket (`run/vsock.sock` inside the jail). The
host connects to it, writes `CONNECT <port>\n`, and waits for `OK <n>\n`. The
host also checks `SO_PEERCRED` on that UNIX socket and refuses a reply whose
uid is not the uid the jail was staged for, so a Firecracker process that
somehow got repointed at a different socket cannot be mistaken for the guest.

**Frame format**: every message, both directions, is a 4-byte big-endian
length prefix followed by that many bytes of UTF-8 JSON (no newline). The
length is checked against a max before any body bytes are read. `AGENT_PORT
= 52`, `MAX_REQUEST_BYTES = 256 KiB` (host to guest), `MAX_REPLY_BYTES = 1 MiB`
(guest to host). The same two constants also bound the API-facing socket: a
request read off `supervisor.sock` is capped at `MAX_REQUEST_BYTES` and its
reply at `MAX_REPLY_BYTES`, independent of the host-to-guest hop.

## 4. Building images by hand

```bash
firecracker/build/build-kernel.sh /srv/magma-fc/images/vmlinux
firecracker/build/build-rootfs.sh /opt/magma-fc/src /srv/magma-fc/images/rootfs.ext4
firecracker/build/build-magma-image.sh /opt/magma/2.29-4 /srv/magma-fc/images/magma-2.29-4.ext4
```

`build-kernel.sh` fetches Firecracker's CI kernel config, adds
`CONFIG_DUMMY`, and builds `vmlinux`; needs a kernel build toolchain.
`build-rootfs.sh` builds a Debian bookworm minbase tree with mmdebstrap, with
the agent baked in; needs root, `mmdebstrap`, `e2fsprogs`. `build-magma-image.sh` packs
one Magma version tree (must contain `magma.exe` and `magmapassfile`) into a
read-only ext4 image for `/dev/vdb`; needs `e2fsprogs`.

None of the three scripts set the final ownership the images need in
`/srv/magma-fc/images` (`root:firecracker`, see section 2); an image built
by hand needs `chgrp firecracker` on top of the `chmod 0640` the scripts
already do, or the jailer cannot read it. `fc-debug-boot.sh` (section 5)
needs root, since it execs Firecracker directly against `/dev/kvm`.

## 5. First boot by hand

```bash
firecracker/host/fc-debug-boot.sh \
  /srv/magma-fc/images/vmlinux /srv/magma-fc/images/rootfs.ext4 \
  /srv/magma-fc/images/magma-2.29-4.ext4 02:00:00:00:00:01
```

This boots with a serial console, no jailer, and no vsock device. A good
boot shows, on the console: the `mount` calls succeeding, then
`init: licence interface lic0 created`. After that the console goes quiet:
the agent is parked in `accept()`, waiting for a vsock connection that can
never arrive here, since this script wires up no vsock device. That silence
is the expected end state, not a hang.

To get a shell instead, set `BOOT_ARGS=init=/bin/bash`:

```bash
BOOT_ARGS=init=/bin/bash firecracker/host/fc-debug-boot.sh \
  /srv/magma-fc/images/vmlinux /srv/magma-fc/images/rootfs.ext4 \
  /srv/magma-fc/images/magma-2.29-4.ext4 02:00:00:00:00:01
```

This drops you at a root shell before `init.sh` has run anything, so repeat
its setup by hand: mount `/proc` and `/sys`, mount tmpfs on `/tmp`
(`size=64m,nosuid,nodev,noexec,mode=1777`) and `/run`
(`size=8m,nosuid,nodev,noexec`), mount `/dev/vdb` on `/opt/magma/current`,
create the licence interface
(`ip link add lic0 type dummy && ip link set lic0 address 02:00:00:00:00:01`),
and set the hostname (`hostname magma-worker`). Then run
`/opt/magma/current/magma.exe -d` and confirm its output lists
`lic0` among the interfaces it checked against the passfile.

These `magma.exe -d` debug steps are unverified: nobody has run them
against a live guest yet. Treat them as a starting point, not a known-good
recipe, until a live run confirms the flag and the output it describes.

## 6. Smoke test through the supervisor

```bash
sudo -u magma-api PYTHONPATH=/opt/magma-fc/src \
  python3 -m firecracker.host.fcrun -e 'print 1+1;'
```

`magma-api` is both a system user and a group, created together by the
Ansible role. The socket is 0660 root:magma-api, so run this as a user in
that group; the role's own smoke-test task runs it as the `magma-api` user
itself. Anyone outside the group gets a connection error, not a protocol
one. `fcrun` defaults to `/run/magma-fc/supervisor.sock`, a 60s timeout, and
a 20 KiB output cap; pass `--code-file` for longer snippets. It exits 2 when
the reply carries an `error` field, 0 otherwise, and prints the full reply
as JSON.

Run this once with the default `guest_seccomp: "on"`. If the reply has
`seccomp_killed: true`, rerun in `"log"` mode and add the syscall it names
to the policy (section 9, Seccomp) rather than leaving the filter off.

## 7. Checks after a job

```bash
systemctl status magma-fc@slot1
ls /srv/magma-fc/jails/firecracker/
journalctl -u magma-fc-supervisor
journalctl -u magma-fc@slot1
```

The guest's serial console is disabled, so a compromised or noisy guest
cannot flood the host log; `journalctl -u magma-fc@<slot>` instead carries
Firecracker's and the jailer's own startup errors (a bad config.json, a
missing image, a jailer chroot failure) for that slot's unit.

After a clean run the unit is inactive, the slot's jail directory is gone
(the supervisor removes it once the unit's cgroup drains), and the slot is
back in the free pool. A slot that fails to drain within the stop grace
period is quarantined: this is in-process supervisor state, not something
systemd or the filesystem shows. The slot is withheld from new jobs until
the supervisor restarts, its jail directory is left in place for
inspection, and the only visible sign is a log line (`... quarantining
...`); `journalctl -u magma-fc-supervisor` is where to look.

The supervisor logs a specific message for most worker_failed outcomes: a
unit start failure logs the `systemctl` output, a vsock connect failure, an
OSError during the request/reply exchange (`guest connection for <slot>
failed: ...`), a missed reply deadline (`guest did not reply before the
deadline on <slot>`), and a bad reply frame each log their own error. An
unexpected exception inside a job on a slot logs `job failed on <slot>`
with a full traceback; one outside that scope (for example before a slot
is taken) is caught in `serve()` and logs `unhandled error running job`.
`bad_request` replies come straight from request validation and log
nothing of their own. Every `run_job()` call, including `bad_request` and
`busy` ones, ends with one info line naming the slot (or `-` when none was
claimed), the outcome, and the free and quarantined slot counts. One line
at startup reports the same two counts after the supervisor resets every
slot it manages.

## 8. Limits and where they are set

- **Guest memory**: `mem_mib` in `/etc/magma-fc/supervisor.json`, copied into
  each job's `config.json` as `machine-config.mem_size_mib`.
- **Unit memory ceiling**: `MemoryMax=1536M` in `magma-fc@.service`, a
  systemd cgroup limit on the whole jailed process tree (jailer, Firecracker,
  the guest), independent of and larger than the guest's own RAM.
- **Unit wall-clock ceiling**: `RuntimeMaxSec=150` in `magma-fc@.service`;
  systemd stops the unit if a job, including guest boot, runs longer than
  this regardless of what the agent or supervisor are doing.
- **Per-job timeout, CPU timeout, output cap**: `timeout`, `cpu_timeout`,
  `output_bytes` on the request, sourced from the API's `MAGMA_TIMEOUT`,
  `MAGMA_CPU_TIMEOUT`, `MAGMA_OUTPUT_KB` settings. `max_timeout` in
  `supervisor.json` is a hard ceiling, not a clamp: a request whose
  `timeout` or `cpu_timeout` exceeds it is rejected outright with
  `bad_request` ("timeout too large"), never forwarded to the guest. The
  agent enforces `cpu_timeout` with `RLIMIT_CPU` and `timeout` with a
  wall-clock kill; the supervisor re-truncates `stdout`/`stderr` to
  `output_bytes`/64 KiB on the way back out, so a guest that lies about
  truncation cannot inflate the reply.
- **Encoded frame budget**: request and reply caps are measured on the
  encoded JSON frame, where a control character takes six bytes, so code
  heavy in control characters can be refused as too large below the API's
  raw input limit; output that overflows is trimmed and marked truncated.
- **Connection cap**: `max_connections` in `supervisor.json` (default
  2 x slots + 2) counts every open API connection; beyond it the supervisor
  answers `busy` and closes. A reply not read within 10 s is dropped.
- **Timeout ceiling vs the unit's RuntimeMaxSec**: `max_timeout + boot_timeout
  + 5` (the longest a job can legitimately take, including the deadline
  margin in `_run_on_slot`) must stay below `RuntimeMaxSec` in
  `magma-fc@.service`, or systemd can kill the unit out from under a job
  that was still within its own limits. At startup `main()` logs a warning,
  naming all three numbers, if that is not the case; it does not refuse to
  start, since `max_timeout` is set by the infra side, not the supervisor.

## 9. Security notes

- The worker host runs with `--no-service-account --no-scopes`: nothing on
  it, compromised guest or compromised host process, carries cloud
  credentials.
- The guest has no NIC. `config.json` lists no `network-interfaces`; the
  only networking device is a dummy interface (`lic0`) carrying the
  licensed MAC, created administratively down, present solely so
  `magma.exe` can read a MAC for its licence check.
- A compromised guest can read the Magma tree image it was built with and
  nothing else: no other host filesystem, no network, no other job's slot.
  Jail images are hard-linked in read-only root:firecracker; only the
  per-job slot directory is chowned to the jailed uid, so a compromised
  guest cannot rewrite a shared master image. The guest itself is the
  isolation boundary, and it is disposable (powered off and discarded after
  one job); the seccomp filter below is defense in depth inside it.
- Metadata server test: from a debug shell inside the guest, confirm the
  instance metadata server is unreachable even without a NIC:
  ```bash
  python3 -c 'import socket; socket.create_connection(("169.254.169.254", 80), timeout=3)'
  ```
  This must fail or time out.

### Seccomp

The agent loads a seccomp allow-list on the Magma child after dropping to
uid 1000 and before exec. The policy is in
`firecracker/guest/seccomp_policy.py`. The supervisor config key
`guest_seccomp` picks the mode and the host passes it to the guest as the
kernel argument `magma.seccomp=`:

- `on` (default): any syscall outside the list kills Magma with SIGSYS.
- `log`: nothing is killed; the host adds `audit=1` and the kernel logs each
  syscall outside the list.
- `off`: no filter.

A reply with `seccomp_killed: true` means the filter killed Magma; it
otherwise looks like a crash (`exit_code: -1`). To find the syscall, set
`guest_seccomp: "log"`, restart the supervisor, rerun the job and read
`seccomp_log` in the reply. `fc-debug-boot.sh` cannot help here: it wires
no vsock device, so the agent never receives a job to run under the filter.
