#!/usr/bin/env python3
"""
cognee_provider.py — graph-memory provider ADAPTER (stub, OFF BY DEFAULT).

Implements the Enclave memory contract (docs/MEMORY-PROVIDERS.md) so a graph backend can be
dropped in behind the SAME interface the wiki/qmd expose, with the SAME per-agent collection
allowlist enforced server-side. This is the plug-point — the graph engine itself (Cognee) is
NOT wired and NOT installed: Cognee is a heavy dependency tree with license/telemetry questions
flagged in MEMORY-PROVIDERS.md, so per the hard rule it is gated on a full security pass before
any install. Until then:

  • get / multi_get  → REAL: return the canonical wiki markdown (the wiki is the source of truth;
                       every accelerator only points INTO it, so get/get-many never need the engine).
  • query (graph-walk) → STUB: returns a clear "graph backend not provisioned" notice (so an agent
                       degrades gracefully instead of erroring), plus the wiki path hits as a floor.
  • ingest / lint / status → REAL contract surface; ingest is a no-op until the engine is enabled.

Enable path (later, after the security pass): implement `_graph_query` against the vetted, pinned,
isolated Cognee install and flip COGNEE_ENABLED=1. The interface above does not change — only the
query backend does. That is the whole point of the contract.

Run as a JSON-RPC plug-point (for an agent's .mcp.json), stdlib only, no dep to vet:
  COGNEE_ALLOWED_COLLECTIONS=knowledge COGNEE_WIKI_ROOT=./home/knowledge \
    python3 cognee_provider.py --http 18183
Contract methods are also importable directly (CogneeProvider) for tests/embedding.
"""
import os, sys, re, json, glob, pathlib, argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTRACT_TOOLS = ["query", "get", "multi_get", "ingest", "lint", "status"]


class CogneeProvider:
    """The memory contract, graph backend stubbed. Allowlist is enforced on every call."""

    def __init__(self, wiki_root, allowed):
        self.wiki_root = pathlib.Path(wiki_root).resolve()
        self.allowed = [c.strip() for c in allowed if c.strip()]
        if not self.allowed:
            raise ValueError("COGNEE_ALLOWED_COLLECTIONS must be a non-empty list (fail-closed).")
        self.enabled = os.environ.get("COGNEE_ENABLED", "0") == "1"

    # ── allowlist + path safety (mirrors qmd_gateway.mjs) ───────────────────────────────────
    def _collection_allowed(self, name):
        return name in self.allowed

    def _safe_path(self, rel):
        """Resolve a doc path under wiki_root for an ALLOWED collection; None if it escapes/denied."""
        p = (self.wiki_root / rel).resolve()
        try:
            p.relative_to(self.wiki_root)
        except ValueError:
            return None
        coll = p.relative_to(self.wiki_root).parts[0] if p != self.wiki_root else ""
        # a top-level file (no collection dir) is allowed only if some allowlist entry covers root-level
        if coll and not self._collection_allowed(coll):
            return None
        return p

    # ── contract: query ─────────────────────────────────────────────────────────────────────
    def query(self, searches=None, collections=None, limit=10):
        searches = searches or []
        terms = " ".join(s.get("query", "") if isinstance(s, dict) else str(s) for s in searches).strip()
        floor = self._wiki_grep(terms, collections, limit)
        if not self.enabled:
            return {
                "backend": "stub",
                "notice": ("Cognee graph backend is NOT provisioned (gated on a security pass: "
                           "license/telemetry review + pin + isolate, per docs/MEMORY-PROVIDERS.md). "
                           "Returning wiki keyword hits as a floor; for graph traversal, enable the "
                           "vetted engine and set COGNEE_ENABLED=1."),
                "hits": floor,
            }
        # When enabled, delegate to the (vetted) graph engine. Not reachable until then.
        return {"backend": "cognee", "hits": self._graph_query(terms, collections, limit)}

    def _graph_query(self, terms, collections, limit):   # pragma: no cover — engine gated/unwired
        raise NotImplementedError("Cognee engine not installed — security pass required before wiring.")

    def _wiki_grep(self, terms, collections, limit):
        kws = [w.lower() for w in re.findall(r"\w{3,}", terms or "")]
        colls = [c for c in (collections or self.allowed) if self._collection_allowed(c)] or self.allowed
        hits = []
        for coll in colls:
            for f in sorted((self.wiki_root / coll).rglob("*.md")) if (self.wiki_root / coll).is_dir() else []:
                try:
                    text = f.read_text(errors="replace")
                except OSError:
                    continue
                score = sum(text.lower().count(k) for k in kws) if kws else 0
                if score:
                    rel = str(f.relative_to(self.wiki_root))
                    snippet = next((ln.strip() for ln in text.splitlines()
                                    if any(k in ln.lower() for k in kws)), "")
                    hits.append({"path": rel, "score": score, "snippet": snippet[:200]})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    # ── contract: get / multi_get (ALWAYS the wiki markdown) ────────────────────────────────
    def get(self, path):
        p = self._safe_path(path)
        if p is None:
            return {"error": f"denied or outside allowlist: {path}"}
        if not p.is_file():
            return {"error": f"not found: {path}"}
        return {"path": path, "markdown": p.read_text(errors="replace")}

    def multi_get(self, pattern):
        out = []
        for f in glob.glob(str(self.wiki_root / pattern), recursive=True):
            rel = os.path.relpath(f, self.wiki_root)
            if self._safe_path(rel) and os.path.isfile(f):
                out.append({"path": rel, "markdown": pathlib.Path(f).read_text(errors="replace")})
        return {"docs": out}

    # ── contract: ingest / lint / status ────────────────────────────────────────────────────
    def ingest(self, source=None):
        if not self.enabled:
            return {"status": "noop", "notice": "graph backend disabled; the wiki remains the store. "
                    "Enable the vetted Cognee engine (COGNEE_ENABLED=1) to cognify."}
        return {"status": "todo", "notice": "wire _graph_ingest against the vetted engine"}

    def lint(self):
        # index-health for the engine; while stubbed, report the corpus is reachable.
        n = sum(1 for c in self.allowed for _ in (self.wiki_root / c).rglob("*.md")) if self.wiki_root.is_dir() else 0
        return {"backend": "stub", "corpus_markdown_files": n, "issues": []}

    def status(self):
        return {
            "provider": "cognee", "enabled": self.enabled,
            "backend": "cognee" if self.enabled else "stub (graph gated on security pass)",
            "wiki_root": str(self.wiki_root), "allowed_collections": self.allowed,
            "tools": CONTRACT_TOOLS,
        }

    # ── JSON-RPC dispatch (the .mcp.json plug-point) ─────────────────────────────────────────
    def dispatch(self, method, params):
        params = params or {}
        if method == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cognee-scoped", "version": "0.1.0-stub"}}
        if method == "tools/list":
            return {"tools": [{"name": t, "description": f"cognee:{t} (memory contract)"} for t in CONTRACT_TOOLS]}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            fn = {"query": self.query, "get": self.get, "multi_get": self.multi_get,
                  "ingest": self.ingest, "lint": self.lint, "status": self.status}.get(name)
            if not fn:
                raise ValueError(f"unknown tool: {name}")
            result = fn(**args) if args else fn()
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        raise ValueError(f"unknown method: {method}")


def _make_handler(provider):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                return self._send(200, {"status": "ok", "provider": "cognee",
                                        "enabled": provider.enabled, "allowed": provider.allowed})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
                result = provider.dispatch(req.get("method"), req.get("params"))
                self._send(200, {"jsonrpc": "2.0", "id": req.get("id"), "result": result})
            except Exception as e:
                self._send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(e)}})
    return H


def build_from_env():
    return CogneeProvider(
        wiki_root=os.environ.get("COGNEE_WIKI_ROOT", "/corpus"),
        allowed=(os.environ.get("COGNEE_ALLOWED_COLLECTIONS", "")).split(","),
    )


def main():
    ap = argparse.ArgumentParser(description="Cognee graph-memory provider (stub, off by default).")
    ap.add_argument("--http", type=int, help="serve the JSON-RPC plug-point on 127.0.0.1:<port>")
    a = ap.parse_args()
    provider = build_from_env()
    if a.http:
        host = os.environ.get("COGNEE_HTTP_HOST", "127.0.0.1")
        srv = ThreadingHTTPServer((host, a.http), _make_handler(provider))
        sys.stderr.write(f"cognee provider (stub) on {host}:{a.http} — allowlist {provider.allowed}, "
                         f"enabled={provider.enabled}\n")
        srv.serve_forever()
    else:
        print(json.dumps(provider.status(), indent=2))


if __name__ == "__main__":
    main()
