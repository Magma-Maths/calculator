# Stage 1: Python dependencies
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install poetry
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction --no-ansi

# Stage 2: nsjail
FROM ubuntu:24.04 AS nsjail-builder
RUN apt-get update && apt-get install -y \
    git build-essential pkg-config autoconf bison flex libtool \
    libprotobuf-dev protobuf-compiler libnl-3-dev libnl-route-3-dev && \
    rm -rf /var/lib/apt/lists/*
# nsjail 3.6, pinned by commit because upstream can move a tag.
RUN git init -q /nsjail && cd /nsjail && \
    git remote add origin https://github.com/google/nsjail.git && \
    git fetch -q --depth 1 origin f78475530b46d0186111a9096b30725f816b55fe && \
    git checkout -q FETCH_HEAD && \
    make

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
