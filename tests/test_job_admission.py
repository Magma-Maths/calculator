import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from app import main
from app.config import Settings
from app.usage_logger import UsageLogger
from firecracker.host import supervisor


class ControlledRunner(supervisor.Runner):
    def __init__(self, slots):
        super().__init__({
            "images": {"kernel": "unused", "rootfs": "unused", "magma": "unused"},
            "slots": [{"name": f"slot{i}"} for i in range(slots)],
            "mem_mib": 512, "vcpus": 1,
        })
        self.started = asyncio.Queue()
        self.finish = asyncio.Event()
        self.releasing = asyncio.Event()
        self.allow_release = asyncio.Event()
        self.allow_release.set()

    async def _run_on_slot(self, slot, nxt, request):
        await self.started.put(request["code"])
        if "hold" in request["code"]:
            await self.finish.wait()
        if "failure" in request["code"]:
            raise RuntimeError("guest connection failed")
        return {
            "stdout": "2\n", "stderr": "", "exit_code": 0,
            "timed_out": "timeout" in request["code"], "truncated": False,
        }

    async def _release(self, slot):
        self.releasing.set()
        await self.allow_release.wait()
        return True


@asynccontextmanager
async def live_api(monkeypatch, socket_path, tmp_path, *, per_ip=None, slots=4, rate=30):
    if per_ip is None:
        monkeypatch.delenv("MAX_CONCURRENT_PER_IP", raising=False)
    else:
        monkeypatch.setenv("MAX_CONCURRENT_PER_IP", str(per_ip))
    settings = Settings(supervisor_socket=str(socket_path), max_concurrent=slots,
                        rate_limit_per_minute=rate)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "semaphore", asyncio.Semaphore(slots))
    monkeypatch.setattr(main, "usage_logger", UsageLogger(str(tmp_path / "usage.jsonl")))
    monkeypatch.setattr(main.rate_limiter, "per_minute", rate)
    runner = ControlledRunner(slots)
    worker = await supervisor.serve(runner, str(socket_path), None)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(
        main.app, log_level="critical", lifespan="off", ws="none",
        forwarded_allow_ips="127.0.0.1",
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=url, timeout=3) as client:
            yield client, runner
    finally:
        runner.finish.set()
        runner.allow_release.set()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        worker.close()
        await worker.wait_closed()
        listener.close()


@pytest.fixture
def socket_path(tmp_path_factory):
    return tmp_path_factory.mktemp("ip") / "sup.sock"


async def post(client, code="quick", ip="192.0.2.1"):
    return await client.post("/execute", json={"code": code}, headers={"X-Forwarded-For": ip})


@pytest.mark.parametrize("cap", [None, 1, 3], ids=["default-two", "one", "three"])
def test_per_ip_cap_preserves_other_clients_and_global_admission(monkeypatch, socket_path, tmp_path, cap):
    async def run():
        async with live_api(monkeypatch, socket_path, tmp_path, per_ip=cap) as (client, runner):
            jobs = []
            try:
                for _ in range(2 if cap is None else cap):
                    jobs.append(asyncio.create_task(post(client, "hold")))
                    await asyncio.wait_for(runner.started.get(), 2)
                rejected = await post(client)
                assert rejected.status_code == 429
                assert "simultaneous" in rejected.json()["error"].lower()
                assert "retry-after" not in rejected.headers
                other = asyncio.create_task(post(client, "hold", "192.0.2.2"))
                jobs.append(other)
                await asyncio.wait_for(runner.started.get(), 2)
                for i in range(4 - len(jobs)):
                    jobs.append(asyncio.create_task(post(client, "hold", f"192.0.2.{i + 3}")))
                    await asyncio.wait_for(runner.started.get(), 2)
                assert (await post(client, ip="192.0.2.99")).status_code == 503
            finally:
                runner.finish.set()
                await asyncio.gather(*jobs)
            assert (await post(client)).status_code == 200
            assert runner.free_slots() == 4

    asyncio.run(run())


@pytest.mark.parametrize("code", ["quick", "timeout", "failure"])
def test_per_ip_capacity_returns_after_worker_outcome(monkeypatch, socket_path, tmp_path, code):
    async def run():
        async with live_api(monkeypatch, socket_path, tmp_path, per_ip=1) as (client, runner):
            runner.allow_release.clear()
            first = asyncio.create_task(post(client, code))
            try:
                await asyncio.wait_for(runner.releasing.wait(), 2)
                assert (await post(client)).status_code == 429
            finally:
                runner.allow_release.set()
                await first
            response = await post(client)
            assert response.status_code == 200
            assert response.json()["success"] is True

    asyncio.run(run())


def test_per_ip_rejections_keep_rate_limit_accounting(monkeypatch, socket_path, tmp_path):
    async def run():
        async with live_api(monkeypatch, socket_path, tmp_path, per_ip=1, rate=3) as (client, runner):
            first = asyncio.create_task(post(client, "hold"))
            try:
                await asyncio.wait_for(runner.started.get(), 2)
                assert (await post(client)).status_code == 429
            finally:
                runner.finish.set()
                await first
            assert (await post(client)).status_code == 200
            rejected = await post(client)
            assert rejected.status_code == 429
            assert rejected.headers["retry-after"] == "60"

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["running", "teardown", "task-cancel"])
def test_disconnect_holds_capacity_until_teardown(monkeypatch, socket_path, tmp_path, phase):
    async def run():
        async with live_api(monkeypatch, socket_path, tmp_path, per_ip=1, slots=2) as (client, runner):
            runner.allow_release.clear()
            if phase == "task-cancel":
                transport = httpx.ASGITransport(app=main.app, client=("192.0.2.1", 1234))
                direct = httpx.AsyncClient(transport=transport, base_url="http://test")
                first = asyncio.create_task(post(direct, "hold"))
            else:
                direct = None
                first = asyncio.create_task(post(client, "quick" if phase == "teardown" else "hold"))
            other = None
            try:
                await asyncio.wait_for(runner.started.get(), 2)
                if phase == "teardown":
                    await asyncio.wait_for(runner.releasing.wait(), 2)
                first.cancel()
                await asyncio.wait_for(runner.releasing.wait(), 2)
                if phase == "task-cancel":
                    first.cancel()
                assert (await post(client)).status_code == 429
                other = asyncio.create_task(post(client, "hold", "192.0.2.2"))
                await asyncio.wait_for(runner.started.get(), 2)
                assert (await post(client, ip="192.0.2.3")).status_code == 503
                runner.allow_release.set()
                async with asyncio.timeout(2):
                    while True:
                        records = (tmp_path / "usage.jsonl").read_text().splitlines()
                        ends = [json.loads(line) for line in records if json.loads(line)["event"] == "end"]
                        if ends:
                            break
                        await asyncio.sleep(0.01)
                assert ends[0]["status"] == 499
                assert ends[0]["reason"] == "disconnected"
                assert (await post(client)).status_code == 200
            finally:
                runner.finish.set()
                runner.allow_release.set()
                await asyncio.gather(first, *([other] if other else []), return_exceptions=True)
                if direct:
                    await direct.aclose()

    asyncio.run(run())
