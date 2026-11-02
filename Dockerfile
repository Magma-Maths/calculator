# Stage 1: Python dependencies
# python:3.12-slim
FROM python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS builder
WORKDIR /app
COPY poetry-requirements.txt ./
RUN pip install --only-binary=:all: --require-hashes -r poetry-requirements.txt
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction --no-ansi

# Stage 2: Runtime
# python:3.12-slim
FROM python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
WORKDIR /app

RUN useradd -m calculator
# A fresh named volume mounted here inherits this ownership, so the
# container, run as calculator, can write usage.jsonl.
RUN mkdir -p /data && chown calculator:calculator /data

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY app/ ./app/
COPY firecracker/__init__.py firecracker/protocol.py ./firecracker/

USER calculator
EXPOSE 8080

# Last, so a new revision reuses every cached layer above.
ARG CALCULATOR_VERSION=unknown
ENV CALCULATOR_VERSION=$CALCULATOR_VERSION
LABEL org.opencontainers.image.revision=$CALCULATOR_VERSION

CMD ["python", "-m", "app.main"]
