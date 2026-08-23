FROM node:22-bookworm-slim AS web-builder

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV NANOCAT_WEB_HOST=0.0.0.0 \
    NANOCAT_WEB_PORT=18790

RUN apt-get update && \
    apt-get install -y --no-install-recommends git openssh-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml uv.lock README.md LICENSE hatch_build.py ./
RUN mkdir -p nanocat && touch nanocat/__init__.py && \
    uv export --frozen --no-dev --no-emit-project \
      --format requirements.txt --output-file /tmp/requirements.txt && \
    uv pip install --system --no-cache -r /tmp/requirements.txt && \
    rm -rf nanocat /tmp/requirements.txt

# Copy the full source and install
COPY nanocat/ nanocat/
COPY --from=web-builder /src/frontend/dist/ nanocat/web/static/
RUN NANOCAT_SKIP_WEB_BUILD=1 uv pip install --system --no-cache --no-deps "."

# Create the runtime data directory used by the launcher.
RUN mkdir -p /app/data

# Web default port
EXPOSE 18790

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -m nanocat.runtime.healthcheck /app/data || exit 1

ENTRYPOINT ["nanocat"]
CMD ["-w", "/app/data"]
