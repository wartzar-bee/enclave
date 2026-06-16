#!/usr/bin/env bash
# tools/gcloud/container-setup.sh — runs IN THE CONTAINER (no sudo, idempotent).
#
# Symlinks gcloud/gsutil/bq shims into ~/.local/bin so they work natively in the
# container, transparently proxying to the Mac host bridge (:18187). Run once per
# fresh container; safe to re-run.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

for t in gcloud gsutil bq; do
  ln -sf "$REPO/tools/gcloud/shims/$t" "$BIN/$t"
  chmod +x "$REPO/tools/gcloud/shims/$t"
  echo "  linked $BIN/$t -> tools/gcloud/shims/$t"
done

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  NOTE: $BIN is not on PATH — add it (it usually already is in this container).";;
esac

echo "==> container gcloud shims installed. Verify the host bridge:"
echo "      python3 tools/gcloud/bridge.py --health"
echo "      gcloud config get-value account"
