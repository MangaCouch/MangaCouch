# The web bundle, the pure-Python wheel and the pinned requirements are platform-independent, so the
# build stages run once on the build host's platform, never under QEMU for another architecture.
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM --platform=$BUILDPLATFORM node:26.13.1-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build

FROM --platform=$BUILDPLATFORM python:3.15.2-slim AS builder
COPY --from=uv /uv /uvx /bin/
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
# The runtime requirements depend only on the lock file, so this layer survives source-only commits.
RUN uv export --frozen --no-dev --no-hashes --no-emit-project --no-annotate --no-header --output-file /requirements.txt
COPY src ./src
COPY hatch_build.py ./
COPY --from=web /build/src/mangacouch/web ./src/mangacouch/web
ARG MANGACOUCH_BUILD_SHA
ARG MANGACOUCH_BUILD_REF=refs/heads/main
RUN uv build --wheel --out-dir /wheels

FROM python:3.15.2-slim
# Build-time only (ARG, not ENV): pip runs as root in the image build, which is intended here.
ARG PIP_ROOT_USER_ACTION=ignore PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/mangacouch
RUN groupadd --gid 10001 mangacouch && useradd --uid 10001 --gid 10001 --no-create-home mangacouch
# Dependencies first, in their own layer: it is reused until uv.lock changes. The per-commit wheel
# follows without dependencies, so a source change reinstalls only MangaCouch itself.
COPY --from=builder /requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=builder /wheels/ /tmp/wheels/
RUN python -m pip install --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels
ENV TMPDIR=/data/state PYTHONUNBUFFERED=1 MANGACOUCH_DB=/data/state/library.sqlite MANGACOUCH_ROOT=/data/books MANGACOUCH_HOST=0.0.0.0 MANGACOUCH_PORT=8780
RUN mkdir -p /data/state /data/books && chown 10001:10001 /data/state /data/books
USER 10001:10001
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD ["python", "-m", "mangacouch", "health"]
ENTRYPOINT ["python", "-m", "mangacouch"]
CMD ["serve"]
