from app.executor import wrap_magma_code, ExecutionResult
from app.config import Settings


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
