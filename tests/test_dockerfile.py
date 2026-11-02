import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_docker_build_inputs_are_immutable():
    dockerfile = (ROOT / "Dockerfile").read_text()

    from_instructions = re.findall(
        r"^FROM .+$",
        dockerfile,
        flags=re.MULTILINE,
    )
    builder_stage = re.fullmatch(
        r"FROM python@sha256:([0-9a-f]{64}) AS builder",
        from_instructions[0],
    )
    assert builder_stage is not None
    python_digest = builder_stage.group(1)
    assert from_instructions == [
        f"FROM python@sha256:{python_digest} AS builder",
        f"FROM python@sha256:{python_digest}",
    ]
    assert dockerfile.count("# python:3.12-slim\nFROM python@sha256:") == 2

    assert "COPY poetry-requirements.txt ./" in dockerfile
    poetry_install = (
        "RUN pip install --only-binary=:all: --require-hashes "
        "-r poetry-requirements.txt"
    )
    pip_install_instructions = re.findall(
        r"^RUN (?:(?:python(?:[0-9.]+)?) -m )?pip(?:[0-9.]+)? install .+$",
        dockerfile,
        flags=re.MULTILINE,
    )
    assert pip_install_instructions == [poetry_install]

    requirements = (ROOT / "poetry-requirements.txt").read_text()
    body = "\n".join(
        line for line in requirements.splitlines() if not line.startswith("#")
    ).strip()
    records = re.split(r"\n(?=[A-Za-z0-9])", body)

    assert any(record.startswith("poetry==1.8.2") for record in records)
    for record in records:
        assert "==" in record.splitlines()[0]
        assert "--hash=sha256:" in record
