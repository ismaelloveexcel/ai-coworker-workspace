# Multi-stage build — keeps runtime image small and secret-free (F12/E8)
# Stage 1: install Python deps
FROM python:3.12-slim AS builder
WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Conditionally install Playwright only when PLAYWRIGHT_ENABLED=true (F12/F44)
ARG PLAYWRIGHT_ENABLED=false
RUN if [ "$PLAYWRIGHT_ENABLED" = "true" ]; then \
      pip install --no-cache-dir --prefix=/install playwright && \
      /install/bin/playwright install chromium --with-deps; \
    fi

# Stage 2: lean runtime image
FROM python:3.12-slim AS runtime
WORKDIR /app

# Non-root user (F12/E8)
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source (no .env, no .git — excluded via .dockerignore)
COPY --chown=app:app . .

# Create data directory with correct ownership
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
