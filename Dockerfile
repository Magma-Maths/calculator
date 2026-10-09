# Stage 1: Python dependencies
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install poetry
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction --no-ansi

# Stage 2: Runtime
FROM python:3.12-slim
WORKDIR /app

RUN useradd -m calculator
# A fresh named volume mounted here inherits this ownership, so the
# container, run as calculator, can write usage.jsonl.
RUN mkdir -p /data && chown calculator:calculator /data

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY app/ ./app/
COPY firecracker/ ./firecracker/

USER calculator
EXPOSE 8080

CMD ["python", "-m", "app.main"]
