# Stage 1: Python dependencies
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install poetry
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi

# Stage 2: nsjail
FROM ubuntu:24.04 AS nsjail-builder
RUN apt-get update && apt-get install -y \
    git build-essential pkg-config \
    libprotobuf-dev protobuf-compiler libnl-3-dev libnl-route-3-dev && \
    rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/google/nsjail.git /nsjail && \
    cd /nsjail && make

# Stage 3: Runtime
FROM python:3.12-slim
WORKDIR /app

# Install nsjail runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnl-3-200 libnl-route-3-200 libprotobuf-lite32t64 && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for Magma (nsjail drops privileges to this user)
RUN useradd -m calculator

COPY --from=nsjail-builder /nsjail/nsjail /usr/local/bin/nsjail
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY app/ ./app/
COPY nsjail.cfg .

# Runs as root (required for nsjail namespace creation)
# nsjail drops privileges to 'calculator' for Magma execution
EXPOSE 8080

CMD ["python", "-m", "app.main"]
