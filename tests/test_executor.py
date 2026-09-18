from app.executor import wrap_magma_code, ExecutionResult
from app.config import Settings
from tests.fake_nsjail import jail_mounts


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
