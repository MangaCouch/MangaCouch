FROM python:3.15.2-slim AS builder
WORKDIR /build
RUN python -m pip install --no-cache-dir uv==0.13.7
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /wheels && uv export --frozen --no-dev --no-hashes --no-emit-project --no-annotate --no-header --output-file /wheels/constraints.txt

FROM python:3.15.2-slim
WORKDIR /opt/mangacouch
COPY --from=builder /wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir -c /tmp/wheels/constraints.txt /tmp/wheels/*.whl && rm -rf /tmp/wheels \
    && groupadd --gid 10001 mangacouch && useradd --uid 10001 --gid 10001 --no-create-home mangacouch
ENV TMPDIR=/data/state PYTHONUNBUFFERED=1 MANGACOUCH_DB=/data/state/library.sqlite MANGACOUCH_ROOT=/data/books MANGACOUCH_HOST=0.0.0.0 MANGACOUCH_PORT=8780
USER 10001:10001
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD ["python", "-m", "mangacouch", "health"]
ENTRYPOINT ["python", "-m", "mangacouch"]
CMD ["serve"]
