# Install the Magma calculator

Thanks for hosting the calculator. This guide takes you from your own Magma
installation to an API backed by Firecracker guests on a Linux host.
Use [FIRECRACKER.md](FIRECRACKER.md) for protocols, troubleshooting, and deeper
operator notes.

1. **Prepare the host.**

   You need x86_64 Linux with working KVM, systemd with `ExitType=cgroup`
   support, and cgroup v2. The launcher requires a unified cgroup hierarchy.
   Install Docker for the API and Python 3.12 at `/usr/bin/python3` for the
   supervisor. The supervisor uses the Python standard library and modules
   from this checkout; API dependencies are installed by the Docker build.
   The supervisor and jailer start as root; Firecracker runs under a dedicated
   unprivileged account.

   Have the Firecracker **v1.17.0** x86_64 release binaries available locally.
   This is the version used by the kernel build script and debug launcher.
   Building images needs Bash, a kernel build toolchain, `make`, `curl`, `tar`
   with xz support, `mmdebstrap`, and `e2fsprogs`. Install the build dependencies
   for your distribution. The rootfs script builds Debian bookworm and needs
   root. The Magma input is a complete version directory containing an
   executable `magma.exe`.

   Run the following host setup commands in a root shell (`sudo -i`). Paths
   under `/path/to/` are placeholders. Keep enough RAM for all configured
   guests, including idle guests, plus the API and host services.

2. **Create the accounts and directories.**

   ```bash
   groupadd --system firecracker
   useradd --system --gid firecracker --no-create-home \
     --shell /usr/sbin/nologin firecracker
   groupadd --system magma-api
   install -d -o root -g root -m 0755 \
     /opt/magma-fc/src /opt/magma-fc/bin /opt/magma-fc/v1.17.0
   install -d -o root -g root -m 0750 /etc/magma-fc
   install -d -o root -g firecracker -m 0750 \
     /srv/magma-fc /srv/magma-fc/images /srv/magma-fc/jails
   cp -a /path/to/calculator/. /opt/magma-fc/src/
   chown -R root:root /opt/magma-fc/src
   chmod -R go-w /opt/magma-fc/src
   install -o root -g root -m 0755 \
     /path/to/release/firecracker-v1.17.0-x86_64 \
     /opt/magma-fc/v1.17.0/firecracker
   install -o root -g root -m 0755 \
     /path/to/release/jailer-v1.17.0-x86_64 \
     /opt/magma-fc/v1.17.0/jailer
   id firecracker
   ```

   If these accounts already exist, check their IDs and ownership before
   reusing them. Record the nonzero numeric UID and GID of `firecracker`.
   The `magma-api` group grants access to the supervisor socket; it does not
   need a host user for the container setup below.

   Images and jails must reside on the **same filesystem**: staging uses
   hard links. Keep their parent directories writable only by root.
   The supervisor creates and owns each slot's working directories as the
   Firecracker UID and GID. Systemd creates `/run/magma-fc` with mode 0755;
   the supervisor creates its socket with mode 0660 and the configured group.
   See [host layout](FIRECRACKER.md#2-host-layout).

3. **Build the guest images.**

   ```bash
   cd /opt/magma-fc/src
   firecracker/build/build-kernel.sh /srv/magma-fc/images/vmlinux
   firecracker/build/build-rootfs.sh \
     /opt/magma-fc/src /srv/magma-fc/images/rootfs.ext4
   firecracker/build/build-magma-image.sh \
     /path/to/magma-version /srv/magma-fc/images/magma.ext4
   chown root:firecracker /srv/magma-fc/images/{vmlinux,rootfs.ext4,magma.ext4}
   chmod 0640 /srv/magma-fc/images/{vmlinux,rootfs.ext4,magma.ext4}
   ```

   The kernel script defaults to Linux 6.1.186 with Firecracker v1.17.0's
   guest configuration. `KERNEL_VERSION` and `FIRECRACKER_TAG` override those
   inputs. Its second argument selects a build directory, defaulting to
   `/var/tmp/magma-fc-kernel`. The Magma image is mounted read-only at
   `/opt/magma/current` in the guest.

   Three optional extension points are available:

   - The kernel script's third argument is a config fragment merged before
     the build; requested settings must survive config resolution.
   - The rootfs script's third argument is an executable site hook. It runs
     as root without arguments, after guest filesystem mounts and before
     the agent starts. A failed hook stops boot.
   - Each slot's `boot_args` string is appended to its guest kernel command
     line at boot.

   See [building images](FIRECRACKER.md#4-building-images-by-hand) for the
   extension argument formats and [first boot](FIRECRACKER.md#5-first-boot-by-hand)
   for an optional console check.

4. **Configure the supervisor and launcher.**

   Save the following as `/etc/magma-fc/supervisor.json`. The example has one
   slot. Replace `1001` in both ID fields with the respective values from
   `id firecracker`; these numbers are placeholders.

   ```json
   {
     "images": {
       "kernel": "/srv/magma-fc/images/vmlinux",
       "rootfs": "/srv/magma-fc/images/rootfs.ext4",
       "magma": "/srv/magma-fc/images/magma.ext4"
     },
     "jail_base": "/srv/magma-fc/jails",
     "slots": [{"name": "slot1", "boot_args": ""}],
     "uid": 1001,
     "gid": 1001,
     "mem_mib": 1024,
     "vcpus": 1,
     "boot_timeout": 20,
     "max_timeout": 120,
     "runtime_max_sec": 150,
     "max_connections": 4,
     "guest_seccomp": "on",
     "socket": "/run/magma-fc/supervisor.sock",
     "socket_group": "magma-api"
   }
   ```

   These are all fields read by the supervisor, including nested fields:

   | Field | Meaning and default |
   | --- | --- |
   | `images.kernel` | Required absolute path to the guest kernel. |
   | `images.rootfs` | Required absolute path to the guest rootfs image. |
   | `images.magma` | Required absolute path to the Magma image. |
   | `jail_base` | Required parent of the per-slot `firecracker/<name>/root` trees. |
   | `slots` | Required list of slots; each can run one job at a time. |
   | `slots[].name` | Required unique slot name. The launcher accepts 1-64 ASCII letters, digits, or hyphens, starting with a letter or digit. |
   | `slots[].boot_args` | Optional extra kernel arguments, default `""`. Up to 1024 ASCII characters as unquoted `key=value` tokens separated by single spaces. |
   | `uid`, `gid` | Required nonzero numeric IDs of the unprivileged Firecracker account and group. |
   | `mem_mib` | Required guest RAM in MiB, reserved for each booted slot. |
   | `vcpus` | Required virtual CPU count per guest. |
   | `boot_timeout` | Required seconds allowed to connect to the guest agent after starting its unit. |
   | `max_timeout` | Ceiling in seconds for both requested wall time and CPU time; default 300. Larger requests are rejected. |
   | `runtime_max_sec` | Unit lifetime in seconds used for idle replacement and job checks; default 150. Must match the unit's `RuntimeMaxSec`. |
   | `max_connections` | Maximum simultaneous API socket connections, including reads and replies; default twice the number of slots plus two. |
   | `guest_seccomp` | Guest filter mode: `on` (default), `log`, or `off`. See [Seccomp](FIRECRACKER.md#seccomp). |
   | `socket` | Required absolute path to the API-facing UNIX socket; its parent must exist. |
   | `socket_group` | Optional host group name for the socket. If absent or empty, no group ownership change is made. |

   Set `max_timeout` explicitly: its default exceeds the shipped unit's
   lifetime. Keep `max_timeout + boot_timeout + 5 < runtime_max_sec`.
   Changing `runtime_max_sec` alone does not change systemd's limit.
   The example fits the shipped `RuntimeMaxSec=150`, `MemoryMax=1536M`,
   and `CPUQuota=100%`. See [limits](FIRECRACKER.md#8-limits-and-where-they-are-set)
   before changing guest resources, timeouts, or slot count.

   Generate `/etc/magma-fc/worker.env` with the same IDs and jail directory:

   ```bash
   cat > /etc/magma-fc/worker.env <<EOF
   FC_UID=$(id -u firecracker)
   FC_GID=$(id -g firecracker)
   FC_DIR=/opt/magma-fc/v1.17.0
   FC_JAIL_BASE=/srv/magma-fc/jails
   EOF
   chown root:root /etc/magma-fc/{supervisor.json,worker.env}
   chmod 0640 /etc/magma-fc/{supervisor.json,worker.env}
   ```

   The supervisor unit sets `MAGMA_FC_CONFIG` to this JSON file and
   `PYTHONPATH` to `/opt/magma-fc/src`. The slot unit reads `worker.env`.
   Keep both files consistent if you change the layout.

5. **Load KVM and start the host services.**

   Load your host's KVM driver and persist it across reboots. The following
   example uses `kvm_intel`; select the driver appropriate to your host.

   ```bash
   KVM_MODULE=kvm_intel
   modprobe "$KVM_MODULE"
   install -d -m 0755 /etc/modules-load.d
   printf '%s\n' "$KVM_MODULE" > /etc/modules-load.d/magma-fc.conf
   test -c /dev/kvm
   test -f /sys/fs/cgroup/cgroup.controllers
   install -o root -g root -m 0755 \
     /opt/magma-fc/src/firecracker/host/magma-fc-launch \
     /opt/magma-fc/bin/magma-fc-launch
   install -o root -g root -m 0644 \
     /opt/magma-fc/src/firecracker/host/magma-fc@.service \
     /opt/magma-fc/src/firecracker/host/magma-fc-supervisor.service \
     /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now magma-fc-supervisor.service
   systemctl status magma-fc-supervisor.service
   ```

   Stop and resolve any failed prerequisite check before starting the service.
   Enable only the supervisor: it starts and stops `magma-fc@<slot>.service`
   instances as needed, including booting idle guests. The launcher is an
   executable used by those units, not a separate service.
   Inspect failures with `journalctl -u magma-fc-supervisor` and
   `journalctl -u magma-fc@slot1`.

6. **Build and run the API container.**

   ```bash
   cd /opt/magma-fc/src
   docker build -t magma-calculator:local .
   docker volume create magma-calculator-data
   API_GID=$(getent group magma-api | cut -d: -f3)
   docker run -d --name magma-calculator --restart unless-stopped \
     --publish 127.0.0.1:8080:8080 \
     --group-add "$API_GID" \
     --mount type=bind,src=/run/magma-fc,dst=/run/magma-fc,readonly \
     --mount type=volume,src=magma-calculator-data,dst=/data \
     --env SUPERVISOR_SOCKET=/run/magma-fc/supervisor.sock \
     --env MAX_CONCURRENT=1 \
     --env FORWARDED_ALLOW_IPS= \
     magma-calculator:local
   ```

   The image runs as `calculator`. `--group-add` grants that user the host
   socket group's numeric ID. Mount the containing directory so the
   container sees replacement sockets after supervisor restarts.
   A fresh named volume inherits `/data` ownership from the image; an
   existing volume must be writable by the container user.

   Set `MAX_CONCURRENT` to your slot count and keep both API timeouts within
   `max_timeout`. This example initially trusts no forwarded client headers;
   configure that setting when adding your proxy. See the
   [README configuration table](README.md#configuration) for API environment
   variables and defaults. Add settings with `--env` or `--env-file` when
   creating the container.

7. **Put a TLS reverse proxy in front.**

   Use a reverse proxy of your choice to terminate TLS for your domain, such
   as `example.org`, and forward requests to the host's `127.0.0.1:8080`.
   Set its request timeout to accommodate job execution and guest startup.
   Have it set `X-Forwarded-For` and configure `FORWARDED_ALLOW_IPS` with only
   the proxy addresses the API sees, so rate limits use client IPs.
   Recreate the API container with those settings and your chosen
   `ALLOWED_ORIGIN`, using the [configuration table](README.md#configuration).

8. **Verify the service.**

   ```bash
   curl --fail-with-body http://127.0.0.1:8080/health
   curl --fail-with-body http://127.0.0.1:8080/health/deep
   curl --fail-with-body http://127.0.0.1:8080/execute \
     -H 'Content-Type: application/json' \
     --data '{"code":"print 1+1;"}'
   ```

   `/health` returns `{"status":"ok"}` when the API responds.
   `/health/deep` should return `status: "ok"`, `probe: "ok"`, and an empty
   `problems` list after executing a guest job. It caches results for
   60 seconds; `busy` is not a successful execution check, so retry when a
   slot is free. `/execute` should return `success: true`, `stdout: "2\n"`,
   and `exit_code: 0`. Repeat these requests against your HTTPS endpoint.
   See [deep health](README.md#get-healthdeep) for optional host status input,
   and [checks after a job](FIRECRACKER.md#7-checks-after-a-job) for diagnostics.

Optionally enable the [submission log](README.md#submission-log) with
`SUBMISSION_LOG_FILE=/data/submissions.jsonl`. It is off by default.
Manage log rotation and deletion yourself, including logs in the `/data`
volume; the service does not rotate them.
