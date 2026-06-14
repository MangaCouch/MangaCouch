#!/usr/bin/env bash
# Container entrypoint: provision on first run (printing credentials to the logs once), then serve.
set -euo pipefail

BASE="${MANGACOUCH_BASE:-/data}"
mkdir -p "$BASE"

if [ ! -f "$BASE/config.toml" ]; then
  echo "==> First run: initialising MangaCouch under $BASE"
  # Honour optional one-shot credential overrides from the environment.
  ARGS=(--base "$BASE" init)
  [ -n "${OWNER_PASSCODE:-}" ] && ARGS+=(--owner-passcode "$OWNER_PASSCODE")
  [ -n "${READER_PASSCODE:-}" ] && ARGS+=(--reader-passcode "$READER_PASSCODE")
  [ -n "${SKIP_TAGDB:-}" ] && ARGS+=(--no-tags)
  uv run mangacouch "${ARGS[@]}"
  echo "==> Credentials above are shown ONCE. Store them now."
fi

if [ "${1:-serve}" = "serve" ]; then
  exec uv run mangacouch --base "$BASE" serve --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
fi

# Any other subcommand is passed straight through (scan, refresh-tags, set-passcode, ...).
exec uv run mangacouch --base "$BASE" "$@"
