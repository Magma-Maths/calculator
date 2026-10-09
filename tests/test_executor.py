from app.config import Settings
from app.magma_cmd import wrap_magma_code


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
