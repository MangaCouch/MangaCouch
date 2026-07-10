#!/usr/bin/env bash
# Container entrypoint: provision on first run (printing credentials to the logs once), then serve.
set -euo pipefail

BASE="${MANGACOUCH_BASE:-/data}"
mkdir -p "$BASE"

if [ ! -f "$BASE/config.toml" ]; then
  echo "==> First run: initialising MangaCouch under $BASE"
  # Honour an optional one-shot passcode override from the environment.
  ARGS=(--base "$BASE" init)
  [ -n "${OWNER_PASSCODE:-}" ] && ARGS+=(--passcode "$OWNER_PASSCODE")
  [ -n "${SKIP_TAGDB:-}" ] && ARGS+=(--no-tags)
  uv run mangacouch "${ARGS[@]}"
  echo "==> Credentials above are shown ONCE. Store them now."
  echo "==> No terminal access? The web UI offers to keep or regenerate the passcode on first visit."
fi

if [ "${1:-serve}" = "serve" ]; then
  exec uv run mangacouch --base "$BASE" serve --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
fi

# Any other subcommand is passed straight through (scan, refresh-tags, set-passcode, ...).
exec uv run mangacouch --base "$BASE" "$@"
