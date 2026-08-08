FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN mkdir -p nanocat && touch nanocat/__init__.py && \
    uv export --frozen --no-dev --extra tui --no-emit-project \
      --format requirements.txt --output-file /tmp/requirements.txt && \
    uv pip install --system --no-cache -r /tmp/requirements.txt && \
    rm -rf nanocat /tmp/requirements.txt

# Copy the full source and install
COPY nanocat/ nanocat/
RUN uv pip install --system --no-cache --no-deps ".[tui]"

# Create the runtime data directory used by the launcher.
RUN mkdir -p /app/data

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["nanocat"]
CMD ["gateway", "-w", "/app/data"]
