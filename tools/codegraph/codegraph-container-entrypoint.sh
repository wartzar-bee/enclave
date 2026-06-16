#!/usr/bin/env bash
# codegraph-container-entrypoint — launcher for the shared codegraph image.
#
# Modes (set CODEGRAPH_MODE):
#   serve   (default)  run the HTTP MCP bridge on :$CODEGRAPH_GW_HTTP_PORT over $CODEGRAPH_CORPUS
#   reembed            (re)build the index: `codegraph init` (first time) or `codegraph sync`
#   shell              drop to bash (debug)
#
# The corpus is mounted at $CODEGRAPH_CORPUS (writable — SQLite needs to manage the index dir).
set -euo pipefail

: "${CODEGRAPH_CORPUS:=/corpus}"

case "${CODEGRAPH_MODE:-serve}" in
  reembed)
    if [ -d "$CODEGRAPH_CORPUS/.codegraph" ]; then
      echo "[codegraph] syncing index for ${CODEGRAPH_CORPUS} ..."
      codegraph sync "$CODEGRAPH_CORPUS"
    else
      echo "[codegraph] building initial index for ${CODEGRAPH_CORPUS} ..."
      codegraph init "$CODEGRAPH_CORPUS"
    fi
    echo "[codegraph] reembed done."
    ;;
  shell)
    exec bash
    ;;
  serve)
    if [ ! -d "$CODEGRAPH_CORPUS/.codegraph" ]; then
      echo "[codegraph] WARNING: no index at ${CODEGRAPH_CORPUS}/.codegraph — run CODEGRAPH_MODE=reembed first (queries will error)." >&2
    fi
    echo "[codegraph] serving HTTP MCP bridge on ${CODEGRAPH_GW_HTTP_HOST:-0.0.0.0}:${CODEGRAPH_GW_HTTP_PORT:-18184} over ${CODEGRAPH_CORPUS}"
    exec node /opt/codegraph-gw/codegraph_gateway.mjs
    ;;
  *)
    echo "FATAL: unknown CODEGRAPH_MODE='${CODEGRAPH_MODE}' (serve|reembed|shell)" >&2
    exit 1
    ;;
esac
