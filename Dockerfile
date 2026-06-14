# MangaCouch — one container running the app + in-process workers, SQLite by default (§7.1).
#
# Stage 1 builds the React PWA; stage 2 installs the Python app (verified wheel set, no source
# builds) with the built SPA baked into the package. Data lives under /data (a volume).

# ---- stage 1: build the SPA ---------------------------------------------------------------
FROM node:20-slim AS web
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---- stage 2: the application -------------------------------------------------------------
FROM python:3.14-slim AS app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

WORKDIR /app

# Resolve dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY src/ ./src/
# Bake the built SPA into the package so FastAPI serves it.
COPY --from=web /frontend/dist/ ./src/mangacouch/web/

RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV MANGACOUCH_BASE=/data
VOLUME ["/data"]
EXPOSE 8000

# Healthcheck hits the always-available API health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
