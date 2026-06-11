FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanocat && touch nanocat/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf nanocat

# Copy the full source and install
COPY nanocat/ nanocat/
RUN uv pip install --system --no-cache .

# Create config directory
RUN mkdir -p /root/.nanocat

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["nanocat"]
CMD ["status"]
