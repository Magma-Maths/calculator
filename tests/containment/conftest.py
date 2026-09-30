import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


DIAGNOSTIC_BYTES = 32 * 1024
IMAGE_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
CGROUP_ROOT = Path("/sys/fs/cgroup")
REQUIRED_CONTROLLERS = {"cpu", "memory", "pids"}
EXPECTED_MEMORY_MAX = 400 * 1024**2
EXPECTED_MEMORY_SWAP_MAX = "0"
EXPECTED_PIDS_MAX = 64
EXPECTED_CPU_MAX = "1000000 1000000"
OUTER_MEMORY_MAX = 3 * 1024**3
OUTER_PIDS_MAX = 320
APPARMOR_PROFILE = "magma-calculator"


class ContainmentBlocked(RuntimeError):
    pass


class CommandFailed(ContainmentBlocked):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"command exited {returncode}: {' '.join(args)}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )


def _bounded(text: str) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= DIAGNOSTIC_BYTES:
        return text
    return data[-DIAGNOSTIC_BYTES:].decode("utf-8", errors="replace")


def run_command(
    args: list[str],
    *,
    timeout: float = 20,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainmentBlocked(f"cannot run {' '.join(args)}: {exc}") from exc
    result.stdout = _bounded(result.stdout)
    result.stderr = _bounded(result.stderr)
    if check and result.returncode != 0:
        raise CommandFailed(args, result.returncode, result.stdout, result.stderr)
    return result


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, Any]
    elapsed: float


@dataclass(frozen=True)
class ProbeRecord:
    mode: str
    status: str
    fields: dict[str, str]

    def integer(self, name: str) -> int:
        try:
            return int(self.fields[name])
        except (KeyError, ValueError) as exc:
            raise AssertionError(f"invalid {name} in {self}") from exc


def parse_probe_line(line: str, expected_mode: str) -> ProbeRecord:
    parts = line.split()
    assert len(parts) >= 3 and parts[0] == "PROBE", line
    assert parts[1] == expected_mode, line
    fields: dict[str, str] = {}
    for field in parts[3:]:
        if "=" in field:
            name, value = field.split("=", 1)
            fields[name] = value
        else:
            fields.setdefault("value", field)
    return ProbeRecord(parts[1], parts[2], fields)


def parse_probe(response: ApiResponse, expected_mode: str) -> ProbeRecord:
    stdout = response.body.get("stdout")
    assert isinstance(stdout, str), response.body
    lines = stdout.rstrip("\n").splitlines()
    assert len(lines) == 1, response.body
    return parse_probe_line(lines[0], expected_mode)


def require_success(response: ApiResponse, mode: str) -> ProbeRecord:
    assert response.status == 200, response.body
    assert response.body.get("success") is True, response.body
    assert response.body.get("exit_code") == 0, response.body
    return parse_probe(response, mode)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _unescape_mount_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


@dataclass(frozen=True)
class MountObservation:
    target: str
    options: frozenset[str]
    filesystem: str
    source: str


def read_mounts(pid: int) -> dict[str, MountObservation]:
    observations: dict[str, MountObservation] = {}
    for line in _read_text(Path(f"/proc/{pid}/mountinfo")).splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        detail = right.split()
        if len(fields) < 6 or len(detail) < 3:
            continue
        target = _unescape_mount_path(fields[4])
        options = set(fields[5].split(",")) | set(detail[2].split(","))
        observations[target] = MountObservation(
            target=target,
            options=frozenset(options),
            filesystem=detail[0],
            source=_unescape_mount_path(detail[1]),
        )
    return observations


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int
    ppid: int
    nspid: tuple[int, ...]
    cgroup: str
    command: str

    def still_exists(self) -> bool:
        try:
            return read_process(self.pid).start_time == self.start_time
        except (OSError, ValueError):
            return False


def read_process(pid: int) -> ProcessIdentity:
    process = Path(f"/proc/{pid}")
    stat = _read_text(process / "stat")
    closing = stat.rfind(")")
    if closing < 0:
        raise ValueError(f"malformed stat for PID {pid}")
    fields = stat[closing + 2 :].split()
    ppid = int(fields[1])
    start_time = int(fields[19])
    status = _read_text(process / "status")
    namespace_line = next(
        (line for line in status.splitlines() if line.startswith("NSpid:")),
        "",
    )
    nspid = tuple(int(value) for value in namespace_line.split()[1:])
    cgroup_lines = _read_text(process / "cgroup").splitlines()
    unified = next((line for line in cgroup_lines if line.startswith("0::")), None)
    if unified is None:
        raise ValueError(f"PID {pid} has no unified cgroup")
    command = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode(
        "utf-8", errors="replace"
    ).strip()
    return ProcessIdentity(pid, start_time, ppid, nspid, unified[3:], command)


def _key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = _read_text(path).splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return values
    for line in lines:
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return values


@dataclass(frozen=True)
class CgroupState:
    path: Path
    members: tuple[int, ...]
    memory_events: dict[str, int]
    pids_events: dict[str, int]
    cpu_stat: dict[str, int]
    memory_max: str | None
    memory_swap_max: str | None
    pids_max: str | None
    cpu_max: str | None


def read_cgroup(path: Path) -> CgroupState | None:
    try:
        members = tuple(int(value) for value in _read_text(path / "cgroup.procs").split())
    except FileNotFoundError:
        return None
    except (PermissionError, OSError, ValueError) as exc:
        raise ContainmentBlocked(f"cannot read cgroup members at {path}: {exc}") from exc

    def optional(name: str) -> str | None:
        try:
            return _read_text(path / name)
        except FileNotFoundError:
            return None
        except (PermissionError, OSError) as exc:
            raise ContainmentBlocked(f"cannot read {path / name}: {exc}") from exc

    return CgroupState(
        path=path,
        members=members,
        memory_events=_key_values(path / "memory.events"),
        pids_events=_key_values(path / "pids.events"),
        cpu_stat=_key_values(path / "cpu.stat"),
        memory_max=optional("memory.max"),
        memory_swap_max=optional("memory.swap.max"),
        pids_max=optional("pids.max"),
        cpu_max=optional("cpu.max"),
    )


class CgroupWatch:
    def __init__(self, root: Path):
        self.root = root
        self.baseline = self._snapshot()
        self.states: dict[Path, list[CgroupState]] = {}
        self.identities: dict[tuple[int, int], ProcessIdentity] = {}
        self.errors: list[Exception] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _snapshot(self) -> dict[Path, CgroupState]:
        if not self.root.is_dir():
            raise ContainmentBlocked(f"service cgroup is unavailable: {self.root}")
        paths = [self.root]
        try:
            paths.extend(path.parent for path in self.root.rglob("cgroup.procs"))
        except (PermissionError, OSError) as exc:
            raise ContainmentBlocked(f"cannot enumerate service cgroups: {exc}") from exc
        states: dict[Path, CgroupState] = {}
        for path in sorted(set(paths)):
            state = read_cgroup(path)
            if state is not None:
                states[path] = state
        return states

    def start(self) -> "CgroupWatch":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise ContainmentBlocked("cgroup observer did not stop")
        if self.errors:
            raise ContainmentBlocked(f"cgroup observer failed: {self.errors[0]}")

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                for path, state in self._snapshot().items():
                    self.states.setdefault(path, []).append(state)
                    for pid in state.members:
                        try:
                            identity = read_process(pid)
                        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                            continue
                        self.identities[(identity.pid, identity.start_time)] = identity
            except Exception as exc:
                self.errors.append(exc)
                return
            self._stop.wait(0.01)

    @property
    def request_paths(self) -> set[Path]:
        return {
            path
            for path in list(self.states)
            if path not in self.baseline and path != self.root / "api"
        }

    def require_request_paths(self) -> set[Path]:
        paths = self.request_paths
        if not paths:
            raise ContainmentBlocked("no per-request cgroup was visible to the external observer")
        return paths

    def counter_delta(self, source: str, key: str) -> int:
        maximum = 0
        observed_paths = self.request_paths
        if source == "memory_events":
            observed_paths.add(self.root)
        for path, samples in self.states.items():
            if path not in observed_paths:
                continue
            baseline = self.baseline.get(path)
            initial = getattr(baseline, source).get(key, 0) if baseline else 0
            maximum = max(
                maximum,
                *(max(0, getattr(sample, source).get(key, 0) - initial) for sample in samples),
            )
        return maximum

    def saw_limits(
        self,
        memory_max: str,
        memory_swap_max: str,
        pids_max: str,
        cpu_max: str,
    ) -> bool:
        return any(
            sample.memory_max == memory_max
            and sample.memory_swap_max == memory_swap_max
            and sample.pids_max == pids_max
            and sample.cpu_max == cpu_max
            for path, samples in self.states.items()
            if path in self.request_paths
            for sample in samples
        )

    def recorded_members(self) -> list[ProcessIdentity]:
        member_ids = {
            pid
            for path, samples in list(self.states.items())
            if path in self.request_paths
            for sample in list(samples)
            for pid in sample.members
        }
        return [
            identity
            for identity in list(self.identities.values())
            if identity.pid in member_ids
        ]


NETWORK_SINK_SCRIPT = r'''
import json
import select
import socket
import struct
import sys

def align(data):
    return data + b"\0" * ((4 - len(data) % 4) % 4)

def attr(kind, data):
    return align(struct.pack("HH", 4 + len(data), kind) + data)

def add_metadata_address():
    route = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    sequence = 1
    address = socket.inet_aton("169.254.169.254")
    body = struct.pack("BBBBI", socket.AF_INET, 32, 0, 0, socket.if_nametoindex("lo"))
    body += attr(1, address) + attr(2, address)
    flags = 1 | 4 | 0x400 | 0x200
    message = struct.pack("IHHII", 16 + len(body), 20, flags, sequence, 0) + body
    route.send(message)
    reply = route.recv(65535)
    _, kind, _, reply_sequence, _ = struct.unpack("IHHII", reply[:16])
    if reply_sequence != sequence or kind != 2:
        raise RuntimeError("unexpected netlink response")
    error = struct.unpack("i", reply[16:20])[0]
    if error not in (0, -17):
        raise OSError(-error, "cannot bind controlled metadata address")

add_metadata_address()
listeners = []
for name, family, address, port in (
    ("net4", socket.AF_INET, "127.0.0.1", 19041),
    ("net6", socket.AF_INET6, "::1", 19042),
    ("metadata", socket.AF_INET, "169.254.169.254", 80),
):
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((address, port))
    listener.listen(8)
    listeners.append((name, listener))
print("SINK " + json.dumps({"event": "ready"}), flush=True)
while True:
    readable, _, _ = select.select([item[1] for item in listeners], [], [], 1)
    for ready in readable:
        name = next(item[0] for item in listeners if item[1] is ready)
        connection, _ = ready.accept()
        connection.close()
        print("SINK " + json.dumps({"event": "accept", "name": name}), flush=True)
'''


NETWORK_CONTROL_SCRIPT = r'''
import socket
import sys

family = socket.AF_INET6 if sys.argv[1] == "inet6" else socket.AF_INET
with socket.socket(family, socket.SOCK_STREAM) as connection:
    connection.settimeout(1)
    connection.connect((sys.argv[2], int(sys.argv[3])))
print("CONTROL_OK")
'''


class NetworkSinks:
    targets = {
        "net4": ("inet", "127.0.0.1", 19041),
        "net6": ("inet6", "::1", 19042),
        "metadata": ("inet", "169.254.169.254", 80),
    }

    def __init__(self, controller: "DockerController"):
        self.controller = controller
        self.name = f"{controller.prefix}-sinks"

    def __enter__(self) -> "NetworkSinks":
        result = self.controller.docker(
            [
                "run",
                "-d",
                "--name",
                self.name,
                "--network",
                f"container:{self.controller.container}",
                "--cap-add",
                "NET_ADMIN",
                "--entrypoint",
                "python",
                self.controller.image_id,
                "-u",
                "-c",
                NETWORK_SINK_SCRIPT,
            ],
            check=False,
        )
        if result.returncode != 0:
            raise ContainmentBlocked(f"cannot start outer-network sinks: {result.stderr}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(event.get("event") == "ready" for event in self.events()):
                return self
            inspect = self.controller.inspect(self.name, check=False)
            if inspect and not inspect[0]["State"]["Running"]:
                break
            time.sleep(0.1)
        logs = self.controller.logs(self.name)
        self.controller.docker(["rm", "-f", self.name], check=False)
        raise ContainmentBlocked(
            "IPv4, IPv6, or controlled metadata sink setup is unavailable: " + logs
        )

    def __exit__(self, _type, _value, _traceback) -> None:
        self.controller.docker(["rm", "-f", self.name], check=False)

    def events(self) -> list[dict[str, Any]]:
        events = []
        for line in self.controller.logs(self.name).splitlines():
            if not line.startswith("SINK "):
                continue
            try:
                events.append(json.loads(line[5:]))
            except json.JSONDecodeError:
                continue
        return events

    def accept_count(self, name: str) -> int:
        return sum(
            event.get("event") == "accept" and event.get("name") == name
            for event in self.events()
        )

    def positive_control(self, name: str) -> int:
        family, address, port = self.targets[name]
        before = self.accept_count(name)
        result = self.controller.docker(
            [
                "run",
                "--rm",
                "--network",
                f"container:{self.controller.container}",
                "--entrypoint",
                "python",
                self.controller.image_id,
                "-c",
                NETWORK_CONTROL_SCRIPT,
                family,
                address,
                str(port),
            ],
            check=False,
        )
        if result.returncode != 0 or "CONTROL_OK" not in result.stdout:
            raise ContainmentBlocked(
                f"outer-network positive control for {name} failed: {result.stderr}"
            )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            after = self.accept_count(name)
            if after > before:
                return after
            time.sleep(0.05)
        raise ContainmentBlocked(f"the controlled {name} sink did not record its positive control")


class DockerController:
    def __init__(
        self,
        image_id: str,
        archive_sha: str,
        fixture_root: Path,
        *,
        magma_timeout: int = 2,
    ):
        self.image_id = image_id
        self.archive_sha = archive_sha
        self.magma_timeout = magma_timeout
        self.fixture_root = fixture_root.resolve()
        selected_fixture = (self.fixture_root / "current").resolve()
        fixture_version = selected_fixture.relative_to(self.fixture_root)
        self.fixture_executable = str(Path("/opt/magma") / fixture_version / "magma.exe")
        suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.prefix = f"calculator-containment-{suffix}"
        self.container = f"{self.prefix}-service"
        self.network = f"{self.prefix}-network"
        self.base_url = ""
        self.init_pid = 0
        self.init_identity: ProcessIdentity | None = None
        self.service_cgroup = Path()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._closed = False
        self._jail_observation: tuple[dict[str, MountObservation], dict[str, str]] | None = None

    @classmethod
    def from_environment(cls, *, magma_timeout: int = 2) -> "DockerController":
        image_id = os.environ.get("CONTAINMENT_IMAGE_ID", "")
        archive_sha = os.environ.get("CONTAINMENT_ARCHIVE_SHA256", "")
        fixture = os.environ.get("CONTAINMENT_FIXTURE_ROOT", "")
        if not IMAGE_RE.fullmatch(image_id):
            raise ContainmentBlocked("CONTAINMENT_IMAGE_ID is missing or malformed")
        if not SHA_RE.fullmatch(archive_sha):
            raise ContainmentBlocked("CONTAINMENT_ARCHIVE_SHA256 is missing or malformed")
        if not fixture:
            raise ContainmentBlocked("CONTAINMENT_FIXTURE_ROOT is missing")
        fixture_root = Path(fixture)
        binary = fixture_root / "versions" / "probe" / "magma.exe"
        current = fixture_root / "current"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ContainmentBlocked(f"static fixture is missing or not executable: {binary}")
        if not current.is_symlink() or os.readlink(current) != "versions/probe":
            raise ContainmentBlocked("fixture current symlink does not select versions/probe")
        return cls(image_id, archive_sha, fixture_root, magma_timeout=magma_timeout)

    def __enter__(self) -> "DockerController":
        if os.geteuid() != 0:
            raise ContainmentBlocked(
                "external procfs and cgroup observation requires a root controller"
            )
        if shutil.which("docker") is None:
            raise ContainmentBlocked("docker CLI is unavailable")
        info = self.docker(["info", "--format", "{{json .ServerVersion}}"], check=False)
        if info.returncode != 0:
            raise ContainmentBlocked(f"Docker daemon access is unavailable: {info.stderr}")
        network = self.docker(["network", "create", self.network], check=False)
        if network.returncode != 0:
            raise ContainmentBlocked(f"Docker network namespace creation failed: {network.stderr}")
        started = self.docker(
            [
                "run",
                "-d",
                "--name",
                self.container,
                "--network",
                self.network,
                "--cap-add",
                "SYS_ADMIN",
                "--cgroupns",
                "private",
                "--security-opt",
                f"apparmor={APPARMOR_PROFILE}",
                "--memory",
                "3g",
                "--pids-limit",
                "320",
                "--tmpfs",
                "/tmp:size=128m",
                "--tmpfs",
                "/data",
                "--mount",
                f"type=bind,source={self.fixture_root},target=/opt/magma,readonly",
                "--publish",
                "127.0.0.1::8080",
                "--env",
                f"MAGMA_TIMEOUT={self.magma_timeout}",
                "--env",
                "MAGMA_CPU_TIMEOUT=10",
                "--env",
                "MAGMA_MEMORY_MB=400",
                "--env",
                "MAGMA_PIDS_MAX=64",
                "--env",
                "MAGMA_CPU_MS_PER_SEC=1000",
                "--env",
                "MAGMA_OUTPUT_KB=20",
                "--env",
                "MAGMA_CAPTURE_KB=256",
                "--env",
                "RATE_LIMIT_PER_MINUTE=1000",
                "--env",
                "RATE_LIMIT_PER_HOUR=1000",
                self.image_id,
            ],
            check=False,
        )
        if started.returncode != 0:
            raise ContainmentBlocked(f"service container did not start: {started.stderr}")
        self._resolve_runtime()
        self._wait_ready()
        self._observe_service_cgroup()
        self._check_runtime()
        self.answer()
        self.jail_observation()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.docker(["rm", "-f", self.container], check=False)
        self.docker(["network", "rm", self.network], check=False)

    def docker(
        self,
        args: list[str],
        *,
        timeout: float = 20,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return run_command(["docker", *args], timeout=timeout, check=check)

    def inspect(self, name: str, *, check: bool = True) -> list[dict[str, Any]]:
        result = self.docker(["inspect", name], check=check)
        if result.returncode != 0:
            return []
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ContainmentBlocked(f"Docker inspect returned invalid JSON for {name}") from exc
        if not isinstance(value, list):
            raise ContainmentBlocked(f"Docker inspect returned the wrong shape for {name}")
        return value

    def logs(self, name: str | None = None) -> str:
        result = self.docker(
            ["logs", "--tail", "100", name or self.container],
            check=False,
        )
        return _bounded(result.stdout + result.stderr)

    def _resolve_runtime(self) -> None:
        inspection = self.inspect(self.container)[0]
        self.init_pid = int(inspection["State"]["Pid"])
        bindings = inspection["NetworkSettings"]["Ports"].get("8080/tcp")
        if not bindings or len(bindings) != 1:
            raise ContainmentBlocked("Docker did not publish exactly one API port")
        binding = bindings[0]
        if binding.get("HostIp") != "127.0.0.1":
            raise ContainmentBlocked("API port is not restricted to host loopback")
        self.base_url = f"http://127.0.0.1:{binding['HostPort']}"

    def _observe_service_cgroup(self) -> None:
        try:
            identity = read_process(self.init_pid)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            raise ContainmentBlocked(f"service init process is not externally visible: {exc}") from exc
        self.init_identity = identity
        relative = identity.cgroup.lstrip("/")
        api = (CGROUP_ROOT / relative).resolve()
        if api.name != "api":
            raise ContainmentBlocked("ready API process is outside the delegated api cgroup")
        root = api.parent
        cgroup_root = CGROUP_ROOT.resolve()
        if root == cgroup_root or cgroup_root not in root.parents:
            raise ContainmentBlocked(f"service cgroup escaped the unified hierarchy: {root}")
        self.service_cgroup = root

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 20
        last_error = ""
        while time.monotonic() < deadline:
            inspection = self.inspect(self.container, check=False)
            if inspection and not inspection[0]["State"]["Running"]:
                break
            try:
                request = urllib.request.Request(self.base_url + "/health", method="GET")
                with urllib.request.urlopen(request, timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.25)
        raise ContainmentBlocked(
            "service readiness failed under production privileges and scoped cgroup delegation: "
            + (self.logs() or last_error)
        )

    def _check_runtime(self) -> None:
        self.require_service_identity()
        inspection = self.inspect(self.container)[0]
        host = inspection["HostConfig"]
        if host.get("Privileged"):
            raise ContainmentBlocked("service unexpectedly runs privileged")
        cap_add = {cap.removeprefix("CAP_") for cap in (host.get("CapAdd") or [])}
        if cap_add != {"SYS_ADMIN"}:
            raise ContainmentBlocked(f"service capability additions differ from production: {cap_add}")
        if host.get("Memory") != OUTER_MEMORY_MAX or host.get("PidsLimit") != OUTER_PIDS_MAX:
            raise ContainmentBlocked("service outer memory or PID limit differs from production")
        if host.get("CgroupnsMode") != "private":
            raise ContainmentBlocked("service does not use a private cgroup namespace")
        if inspection.get("AppArmorProfile") != APPARMOR_PROFILE:
            raise ContainmentBlocked("service does not select the calculator AppArmor profile")
        apparmor = _read_text(Path(f"/proc/{self.init_pid}/attr/current")).strip()
        if apparmor != f"{APPARMOR_PROFILE} (enforce)":
            raise ContainmentBlocked(f"service AppArmor profile is not enforced: {apparmor}")
        fixture_mounts = [
            mount
            for mount in inspection.get("Mounts", [])
            if mount.get("Destination") == "/opt/magma"
        ]
        if len(fixture_mounts) != 1 or fixture_mounts[0].get("RW") is not False:
            raise ContainmentBlocked("fixture mount is missing or writable")
        tmpfs = host.get("Tmpfs") or {}
        if "/tmp" not in tmpfs or "/data" not in tmpfs:
            raise ContainmentBlocked("outer temporary mounts differ from the acceptance runtime")
        status = _read_text(Path(f"/proc/{self.init_pid}/status"))
        cap_line = next((line for line in status.splitlines() if line.startswith("CapEff:")), "")
        if not cap_line:
            raise ContainmentBlocked("effective service capabilities are not externally readable")
        effective = int(cap_line.split()[1], 16)
        if not effective & (1 << 21):
            raise ContainmentBlocked("CAP_SYS_ADMIN is absent from the service effective set")
        unexpected = {"CAP_NET_ADMIN": 12, "CAP_SYS_MODULE": 16, "CAP_SYS_PTRACE": 19}
        present = [name for name, bit in unexpected.items() if effective & (1 << bit)]
        if present:
            raise ContainmentBlocked(f"service has unexpected effective capabilities: {present}")
        for namespace in ("net", "pid", "mnt", "uts"):
            try:
                service_ns = os.readlink(f"/proc/{self.init_pid}/ns/{namespace}")
                host_ns = os.readlink(f"/proc/self/ns/{namespace}")
            except OSError as exc:
                raise ContainmentBlocked(f"cannot observe {namespace} namespace: {exc}") from exc
            if service_ns == host_ns:
                raise ContainmentBlocked(f"service did not receive a distinct {namespace} namespace")
        state = read_cgroup(self.service_cgroup)
        if state is None:
            raise ContainmentBlocked("service cgroup disappeared during preflight")
        if state.memory_max != str(OUTER_MEMORY_MAX) or state.pids_max != str(OUTER_PIDS_MAX):
            raise ContainmentBlocked("observed outer cgroup limits differ from production")
        available = set(_read_text(self.service_cgroup / "cgroup.controllers").split())
        enabled = set(_read_text(self.service_cgroup / "cgroup.subtree_control").split())
        if not REQUIRED_CONTROLLERS <= available or not REQUIRED_CONTROLLERS <= enabled:
            raise ContainmentBlocked("memory, pids, and cpu are not delegated to request cgroups")
        if state.members:
            raise ContainmentBlocked("service cgroup root still contains processes")
        api_state = read_cgroup(self.service_cgroup / "api")
        if api_state is None or self.init_pid not in api_state.members:
            raise ContainmentBlocked("API process is not in the delegated api cgroup")
        counter_contract = {
            "memory.events": "oom",
            "pids.events": "max",
            "cpu.stat": "usage_usec",
        }
        for filename, required_key in counter_contract.items():
            counters = _key_values(self.service_cgroup / filename)
            if required_key not in counters:
                raise ContainmentBlocked(f"required cgroup counter is unreadable: {filename}")
        help_result = self.docker(
            ["run", "--rm", "--entrypoint", "/usr/local/bin/nsjail", self.image_id, "-h"],
            check=False,
        )
        help_text = help_result.stdout + help_result.stderr
        if help_result.returncode != 0 or "usage:" not in help_text.lower() or "nsjail" not in help_text.lower():
            raise ContainmentBlocked("verified image cannot report the real nsjail help banner")
        header = self.docker(
            [
                "run",
                "--rm",
                "--entrypoint",
                "python",
                self.image_id,
                "-c",
                "print(open('/usr/local/bin/nsjail','rb').read(4).hex())",
            ],
            check=False,
        )
        if header.returncode != 0 or header.stdout.strip() != "7f454c46":
            raise ContainmentBlocked("verified image nsjail path is not an ELF binary")
        print(
            "CONTAINMENT_RUNTIME "
            + json.dumps(
                {
                    "archive_sha256": self.archive_sha,
                    "image_id": self.image_id,
                    "container": self.container,
                    "init_pid": self.init_pid,
                    "service_cgroup": str(self.service_cgroup),
                    "cap_eff": f"{effective:016x}",
                    "apparmor": apparmor,
                    "nsjail_help_banner": help_text.splitlines()[0],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def execute(self, mode: str, argument: str | None = None, *, timeout: float = 12) -> ApiResponse:
        code = f"CALC_PROBE {mode}" + (f" {argument}" if argument is not None else "")
        payload = json.dumps({"code": code}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/execute",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                body_bytes = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body_bytes = exc.read(2 * 1024 * 1024)
        except (OSError, urllib.error.URLError) as exc:
            raise AssertionError(f"HTTP request for {mode} failed: {exc}") from exc
        elapsed = time.monotonic() - started
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"HTTP request for {mode} returned invalid JSON") from exc
        assert isinstance(body, dict), body
        return ApiResponse(status, body, elapsed)

    def execute_async(
        self,
        mode: str,
        argument: str | None = None,
        *,
        timeout: float = 12,
    ) -> concurrent.futures.Future[ApiResponse]:
        return self._executor.submit(self.execute, mode, argument, timeout=timeout)

    def answer(self) -> ApiResponse:
        response = self.execute("answer")
        record = require_success(response, "answer")
        assert record.status == "OK", response.body
        assert record.fields == {"value": "42"}, response.body
        assert response.body["stdout"] == "PROBE answer OK 42\n", response.body
        return response

    def watch(self) -> CgroupWatch:
        self.require_service_identity()
        return CgroupWatch(self.service_cgroup).start()

    def require_service_identity(self) -> ProcessIdentity:
        identity = self.init_identity
        if identity is None or not identity.still_exists():
            raise ContainmentBlocked("service init PID disappeared or was reused")
        current = read_process(identity.pid)
        if current.cgroup != identity.cgroup or current.nspid != identity.nspid:
            raise ContainmentBlocked("service init PID changed cgroup or namespace identity")
        return current

    def live_service_processes(self) -> list[ProcessIdentity]:
        self.require_service_identity()
        identities = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                identity = read_process(int(entry.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
                continue
            try:
                process_cgroup = (CGROUP_ROOT / identity.cgroup.lstrip("/")).resolve()
            except OSError:
                continue
            if process_cgroup == self.service_cgroup or self.service_cgroup in process_cgroup.parents:
                identities.append(identity)
        return identities

    def is_fixture_process(self, identity: ProcessIdentity) -> bool:
        command = identity.command.split()
        return bool(command) and command[0] == self.fixture_executable

    def wait_for_abstract_listener(self, nonce: str, *, timeout: float = 2) -> ProcessIdentity:
        marker = f"@calc-probe-{nonce}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for identity in self.live_service_processes():
                if not self.is_fixture_process(identity):
                    continue
                try:
                    sockets = _read_text(Path(f"/proc/{identity.pid}/net/unix"))
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                if marker in sockets and identity.still_exists():
                    return identity
            time.sleep(0.01)
        raise ContainmentBlocked("abstract listener was not externally visible before overlap")

    def wait_for_tmp_marker(self, nonce: str, *, timeout: float = 2) -> ProcessIdentity:
        deadline = time.monotonic() + timeout
        marker = f"calc-probe-{nonce}"
        while time.monotonic() < deadline:
            for identity in self.live_service_processes():
                if not self.is_fixture_process(identity):
                    continue
                path = Path(f"/proc/{identity.pid}/root/tmp/{marker}")
                try:
                    exists = path.is_file()
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                if exists and identity.still_exists():
                    return identity
            time.sleep(0.01)
        raise ContainmentBlocked("temporary marker was not externally visible before overlap")

    def jail_observation(self) -> tuple[dict[str, MountObservation], dict[str, str]]:
        if self._jail_observation is not None:
            return self._jail_observation
        nonce = f"mount{uuid.uuid4().hex[:12]}"
        future = self.execute_async("tmp_write", f"{nonce}:2000")
        deadline = time.monotonic() + 2
        fixture: ProcessIdentity | None = None
        nsjail: ProcessIdentity | None = None
        while time.monotonic() < deadline:
            processes = self.live_service_processes()
            fixture = next(
                (process for process in processes if self.is_fixture_process(process)),
                None,
            )
            nsjail = next(
                (
                    process
                    for process in processes
                    if process.command.split()
                    and process.command.split()[0] in ("nsjail", "/usr/local/bin/nsjail")
                ),
                None,
            )
            if fixture is not None and nsjail is not None:
                break
            time.sleep(0.02)
        if fixture is None or nsjail is None:
            future.result(timeout=8)
            raise ContainmentBlocked("live nsjail and fixture processes are not externally visible")
        try:
            mounts = read_mounts(fixture.pid)
            namespaces = {
                name: os.readlink(f"/proc/{fixture.pid}/ns/{name}")
                for name in ("net", "pid", "mnt", "uts")
            }
        except (FileNotFoundError, PermissionError, OSError) as exc:
            future.result(timeout=8)
            raise ContainmentBlocked(f"cannot inspect the live jail: {exc}") from exc
        response = future.result(timeout=8)
        record = require_success(response, "tmp_write")
        assert record.status == "OK", response.body
        service_namespaces = {
            name: os.readlink(f"/proc/{self.init_pid}/ns/{name}")
            for name in ("net", "pid", "mnt", "uts")
        }
        for name, observed in namespaces.items():
            assert observed != service_namespaces[name], f"nsjail did not create a new {name} namespace"
        print(
            "CONTAINMENT_JAIL "
            + json.dumps(
                {
                    "mounts": {
                        target: {
                            "filesystem": mount.filesystem,
                            "options": sorted(mount.options),
                            "source": mount.source,
                        }
                        for target, mount in sorted(mounts.items())
                    },
                    "namespaces": namespaces,
                    "fixture_pid": fixture.pid,
                    "fixture_start_time": fixture.start_time,
                    "fixture_nspid": fixture.nspid,
                    "nsjail_pid": nsjail.pid,
                    "nsjail_start_time": nsjail.start_time,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        self._jail_observation = (mounts, namespaces)
        return self._jail_observation


@pytest.fixture(scope="session")
def controller():
    instance: DockerController | None = None
    try:
        instance = DockerController.from_environment()
        with instance:
            yield instance
    except ContainmentBlocked as exc:
        pytest.fail(f"BLOCKED: {exc}", pytrace=False)
    finally:
        if instance is not None:
            try:
                instance.cleanup()
            except ContainmentBlocked:
                pass


@pytest.fixture
def output_cleanup_controller():
    instance: DockerController | None = None
    try:
        instance = DockerController.from_environment(magma_timeout=10)
        with instance:
            yield instance
    except ContainmentBlocked as exc:
        pytest.fail(f"BLOCKED: {exc}", pytrace=False)
    finally:
        if instance is not None:
            try:
                instance.cleanup()
            except ContainmentBlocked:
                pass


@pytest.fixture(scope="session")
def network_sinks(controller: DockerController):
    try:
        with NetworkSinks(controller) as sinks:
            yield sinks
    except ContainmentBlocked as exc:
        pytest.fail(f"BLOCKED: {exc}", pytrace=False)
