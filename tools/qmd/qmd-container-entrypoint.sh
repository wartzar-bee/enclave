#!/usr/bin/env bash
# qmd-container-entrypoint — fail-closed launcher for the containerized qmd gateway.
#
# Modes (set QMD_MODE):
#   serve   (default)  run the scoped HTTP gateway on :$QMD_GW_HTTP_PORT
#   reembed            (re)build the index from /corpus into /index, then exit
#   shell              drop to bash (debug)
#
# The gateway REQUIRES QMD_ALLOWED_COLLECTIONS (comma-separated) — it refuses to start
# without one (fail-closed, server-side allowlist). Set it per-agent in compose/.env.
set -euo pipefail

QMD_BIN="/opt/qmd-gw/node_modules/.bin/qmd"
GW="/opt/qmd-gw/qmd_gateway.mjs"

case "${QMD_MODE:-serve}" in
  reembed)
    : "${QMD_CORPUS:=/corpus}"
    echo "[qmd] indexing ${QMD_CORPUS} -> ${QMD_DB} ..."
    # `qmd index` ingests markdown; `qmd embed` builds the vector index. CPU by default
    # (QMD_FORCE_CPU=1); a GPU compose profile can drop that on Linux+NVIDIA.
    "$QMD_BIN" index "$QMD_CORPUS"
    "$QMD_BIN" embed
    echo "[qmd] reembed done."
    ;;
  shell)
    exec bash
    ;;
  serve)
    if [ -z "${QMD_ALLOWED_COLLECTIONS:-}" ]; then
      echo "FATAL: QMD_ALLOWED_COLLECTIONS is unset — the gateway is fail-closed; set a" >&2
      echo "       comma-separated per-agent allowlist (e.g. QMD_ALLOWED_COLLECTIONS=notes,research)." >&2
      exit 1
    fi
    if [ ! -f "$QMD_DB" ]; then
      echo "[qmd] WARNING: index $QMD_DB not found — run QMD_MODE=reembed first (queries will be empty)." >&2
    fi
    echo "[qmd] serving scoped gateway on ${QMD_GW_HTTP_HOST:-0.0.0.0}:${QMD_GW_HTTP_PORT:-18182} (allowlist: ${QMD_ALLOWED_COLLECTIONS})"
    exec node "$GW"
    ;;
  *)
    echo "FATAL: unknown QMD_MODE='${QMD_MODE}' (serve|reembed|shell)" >&2
    exit 1
    ;;
esac
