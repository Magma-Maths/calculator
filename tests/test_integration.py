"""Integration tests using fake_magma and real_magma fixtures.

These exercise the full pipeline: FastAPI → executor → subprocess → parser.
- fake_magma tests use fake_magma.py (always runs)
- real_magma tests use the real Magma binary (skipped if not available)
"""

import pytest


@pytest.mark.parametrize(
    "output, expected, truncated",
    [
        ("\u00e9" * 512, "\u00e9" * 512, True),
        ("x" + "\u00e9" * 512, "x" + "\u00e9" * 511, True),
        ("x" + "\U0001f600" * 256, "x" + "\U0001f600" * 255, True),
        ("x" + "\u00e9" * 511, "x" + "\u00e9" * 511 + "\n", False),
    ],
    ids=["newline-over-cap", "two-byte-cut", "four-byte-cut", "exact-cap"],
)
def test_execute_utf8_output_cap(fake_magma, monkeypatch, output, expected, truncated):
    from app import main
    from app.parser import TRUNCATION_WARNING

    monkeypatch.setattr(main.settings, "magma_output_kb", 1)
    resp = fake_magma.post("/execute", json={"code": f'print "{output}";'})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["stdout"].encode("utf-8")) <= 1024
    assert data["stdout"] == expected
    assert data["truncated"] is truncated
    assert data["success"] is not truncated
    assert data["warnings"] == []
    if truncated:
        assert data["error"] == TRUNCATION_WARNING
    else:
        assert "error" not in data


def test_simple_arithmetic(fake_magma):
    resp = fake_magma.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"] == "2\n"
    assert data["warnings"] == []


def test_variable_and_print(fake_magma):
    resp = fake_magma.post("/execute", json={"code": "x := 42;\nprint x;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"] == "42\n"


def test_multiple_prints(fake_magma):
    code = "print 1;\nprint 2;\nprint 3;"
    resp = fake_magma.post("/execute", json={"code": code})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"] == "1\n2\n3\n"


def test_error_handling(fake_magma):
    resp = fake_magma.post("/execute", json={"code": "print undefined_var;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "User error" in data["stdout"]
    assert "error" in data["error"].lower()
    assert data["error"] not in data["warnings"]


def test_full_response_structure(fake_magma):
    resp = fake_magma.post("/execute", json={"code": "print 7*6;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"] == "42\n"
    assert data["exit_code"] == 0
    assert data["truncated"] is False
    assert data["warnings"] == []

    magma = data["magma"]
    # Version/seed/time/memory are randomized, just check they're present and valid
    assert magma["version"] is not None
    assert magma["version"].startswith("2.")
    assert isinstance(magma["seed"], int)
    assert magma["seed"] > 0
    assert isinstance(magma["time_sec"], float)
    assert magma["time_sec"] >= 0
    assert magma["memory"] is not None
    assert "MB" in magma["memory"]


def test_empty_code(fake_magma):
    resp = fake_magma.post("/execute", json={"code": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"] == ""


# --- Real Magma tests (skipped if Magma not installed) ---


def test_real_simple_arithmetic(real_magma):
    resp = real_magma.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"].strip() == "2"


def test_real_variable_and_print(real_magma):
    resp = real_magma.post("/execute", json={"code": "x := 42;\nprint x;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"].strip() == "42"


def test_real_multiple_prints(real_magma):
    code = "print 1;\nprint 2;\nprint 3;"
    resp = real_magma.post("/execute", json={"code": code})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    lines = [l for l in data["stdout"].strip().split("\n") if l]
    assert lines == ["1", "2", "3"]


def test_real_response_structure(real_magma):
    resp = real_magma.post("/execute", json={"code": "print 7*6;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"].strip() == "42"
    assert data["exit_code"] == 0
    assert data["truncated"] is False
    assert data["warnings"] == []

    magma = data["magma"]
    assert magma["version"] is not None
    assert magma["version"].startswith("2.")
    assert isinstance(magma["seed"], int)
    assert magma["seed"] > 0
    assert isinstance(magma["time_sec"], float)
    assert magma["time_sec"] >= 0
    assert magma["memory"] is not None
    assert "MB" in magma["memory"]


def test_real_factorial(real_magma):
    resp = real_magma.post("/execute", json={"code": "print Factorial(10);"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"].strip() == "3628800"


def test_real_polynomial(real_magma):
    code = "R<x> := PolynomialRing(Integers());\nprint (x+1)^3;"
    resp = real_magma.post("/execute", json={"code": code})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "x^3" in data["stdout"]
