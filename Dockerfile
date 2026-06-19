# syntax=docker/dockerfile:1

# ---- builder: resolve locked deps into /app/.venv with uv ----
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0
# Only the lockfiles -> this layer caches unless deps change. Because
# tool.uv.package = false, uv installs dependencies only (no project build),
# so the bcdm/ source is NOT needed in this stage.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---- runtime ----
FROM python:3.14-slim
WORKDIR /app
RUN useradd -m -u 1000 app && mkdir -p /data && chown app:app /data
COPY --from=builder /app/.venv /app/.venv
COPY bcdm ./bcdm
ENV PATH="/app/.venv/bin:$PATH" \
    BCDM_DB_PATH=/data/bcdm.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/static/styles.css', timeout=4).status==200 else 1)"
CMD ["python", "-m", "bcdm"]
