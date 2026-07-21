#!/usr/bin/env node
// qmd_gateway.mjs — Enclave scoped MCP gateway over qmd.
//
// A drop-in replacement for `qmd mcp` that enforces a per-agent collection
// allowlist server-side, on EVERY method (fail-closed). qmd stays pinned and
// unmodified — this imports its public createStore() API and re-registers the
// same tools with the allowlist applied. See docs/MEMORY-MODES.md.
//
// Transports:
//   - stdio (default): for a local MCP client that spawns this process.
//   - HTTP  (set QMD_GW_HTTP_PORT): Streamable HTTP on 127.0.0.1:<port>, so a
//     containerized agent reaches it via host.docker.internal:<port> (same shape
//     as qmd's own :18181). Binds IPv4 loopback directly — no socat needed.
//
// Env:
//   QMD_ALLOWED_COLLECTIONS  (required) comma-separated allowlist. Empty/unset = refuse to start.
//   QMD_DB                   (optional) path to the shared index.sqlite.
//   QMD_GW_HTTP_PORT         (optional) if set, serve HTTP on this port instead of stdio.
//
// Launch via the qmd-gateway wrapper (sets NODE_PATH + node 26).

import {
  createStore,
  extractSnippet,
  addLineNumbers,
  DEFAULT_MULTI_GET_MAX_BYTES,
} from "@tobilu/qmd";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import os from "node:os";
import path from "node:path";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";

// qmd's getDefaultDbPath() is gated behind an internal "production mode" flag not
// exposed via the public SDK, so resolve the standard index location directly.
function defaultDbPath() {
  const cache = process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache");
  return path.join(cache, "qmd", "index.sqlite");
}

// --- allowlist (fail-closed) -------------------------------------------------
const ALLOWED = (process.env.QMD_ALLOWED_COLLECTIONS || "")
  .split(",").map((s) => s.trim()).filter(Boolean);
if (ALLOWED.length === 0) {
  console.error("FATAL: QMD_ALLOWED_COLLECTIONS must be a non-empty comma-separated list (fail-closed).");
  process.exit(1);
}
const allowedSet = new Set(ALLOWED);

const dbPath = process.env.QMD_DB || process.env.INDEX_PATH || defaultDbPath();
const store = await createStore({ dbPath }); // shared, stateless SQLite — safe for concurrent sessions

const allCols = await store.listCollections();
for (const name of ALLOWED) {
  if (!allCols.find((c) => c.name === name)) {
    console.error(`WARN: allowed collection '${name}' is not present in the index at ${dbPath}`);
  }
}
const allowedPaths = allCols.filter((c) => allowedSet.has(c.name)).map((c) => c.pwd).filter(Boolean);

function pathInAllowed(filepath) {
  if (!filepath) return false;
  return allowedPaths.some((p) => filepath === p || filepath.startsWith(p.endsWith("/") ? p : p + "/"));
}
function docAllowed(collectionName, filepath) {
  if (collectionName) return allowedSet.has(collectionName);
  return pathInAllowed(filepath);
}
function encodeQmdPath(p) {
  return String(p).split("/").map(encodeURIComponent).join("/");
}
function formatSearchSummary(results, q) {
  if (results.length === 0) return `No results for "${q}".`;
  const lines = [`${results.length} result(s):`];
  for (const r of results) {
    const first = (r.snippet || "").split("\n").find((l) => l.trim()) || "";
    lines.push(`- [${r.score}] ${r.file}:${r.line}  ${r.title}`.trim());
    if (first) lines.push(`    ${first.slice(0, 200)}`);
  }
  return lines.join("\n");
}

const instructions = [
  "qmd knowledge base (scoped). You may ONLY search these collections:",
  `  ${ALLOWED.join(", ")}`,
  "Other collections are not accessible from this agent. Use `query` to search,",
  "`get`/`multi_get` to read documents, `status` for index health.",
].join("\n");

const subSearchSchema = z.object({
  type: z.enum(["lex", "vec", "hyde"]).describe("lex = BM25 keywords; vec = semantic question; hyde = hypothetical answer passage"),
  query: z.string().describe("Query text. lex: keywords/\"phrases\"/-negation. vec: natural-language question. hyde: 50-100 word answer passage."),
});

// Register the four scoped tools on a fresh McpServer (one per session for HTTP).
function buildServer() {
  const server = new McpServer({ name: "qmd-scoped", version: "1.0.0" }, { instructions });

  server.registerTool("query", {
    title: "Query",
    description: "Search the knowledge base with typed sub-queries (lex/vec/hyde). First sub-query gets 2x weight. Scoped to this agent's permitted collections.",
    annotations: { readOnlyHint: true, openWorldHint: false },
    inputSchema: {
      searches: z.array(subSearchSchema).min(1).max(10).describe("Typed sub-queries. First gets 2x weight."),
      limit: z.number().optional().default(10),
      minScore: z.number().optional().default(0),
      candidateLimit: z.number().optional(),
      collections: z.array(z.string()).optional().describe("Filter within permitted collections (OR match). Disallowed names are ignored."),
      intent: z.string().optional().describe("Background context to disambiguate the query."),
      rerank: z.boolean().optional().default(true),
    },
  }, async ({ searches, limit, minScore, candidateLimit, collections, intent, rerank }) => {
    const queries = searches.map((s) => ({ type: s.type, query: s.query }));
    const requested = collections ?? ALLOWED;
    const effective = requested.filter((c) => allowedSet.has(c)); // ENFORCE
    if (effective.length === 0) {
      return { content: [{ type: "text", text: "No permitted collections in scope for this query." }], structuredContent: { results: [] } };
    }
    const results = await store.search({ queries, collections: effective, limit, minScore, candidateLimit, rerank, intent });
    const primaryQuery = searches.find((s) => s.type === "lex")?.query
      || searches.find((s) => s.type === "vec")?.query || searches[0]?.query || "";
    const filtered = results.map((r) => {
      const { line, snippet } = extractSnippet(r.body, primaryQuery, 300, r.bestChunkPos, r.bestChunk.length, intent);
      return {
        docid: `#${r.docid}`, file: r.displayPath, title: r.title,
        score: Math.round(r.score * 100) / 100, context: r.context, line,
        snippet: addLineNumbers(snippet, line),
      };
    });
    return { content: [{ type: "text", text: formatSearchSummary(filtered, primaryQuery) }], structuredContent: { results: filtered } };
  });

  server.registerTool("get", {
    title: "Get Document",
    description: "Retrieve a document by file path or docid (from search results). Supports ':line' and ':from:count' suffixes. Scoped to permitted collections.",
    annotations: { readOnlyHint: true, openWorldHint: false },
    inputSchema: {
      file: z.string().describe("File path or docid (#abc123). Optional ':100' or ':100:40' line-range suffix."),
      fromLine: z.number().optional(),
      maxLines: z.number().optional(),
      lineNumbers: z.boolean().optional().default(true),
    },
  }, async ({ file, fromLine, maxLines, lineNumbers }) => {
    let parsedFromLine = fromLine, parsedMaxLines = maxLines, lookup = file;
    const rangeMatch = lookup.match(/:(\d+):(\d+)$/);
    if (rangeMatch) {
      if (parsedFromLine === undefined) parsedFromLine = parseInt(rangeMatch[1], 10);
      if (parsedMaxLines === undefined) parsedMaxLines = parseInt(rangeMatch[2], 10);
      lookup = lookup.slice(0, -rangeMatch[0].length);
    } else {
      const colonMatch = lookup.match(/:(\d+)$/);
      if (colonMatch && colonMatch[1] && parsedFromLine === undefined) {
        parsedFromLine = parseInt(colonMatch[1], 10);
        lookup = lookup.slice(0, -colonMatch[0].length);
      }
    }
    if (parsedFromLine !== undefined) parsedFromLine = Math.max(1, parsedFromLine);
    const result = await store.get(lookup, { includeBody: false });
    if ("error" in result || !docAllowed(result.collectionName, result.filepath)) { // ENFORCE
      let msg = `Document not found: ${file}`;
      if ("error" in result && result.similarFiles.length > 0) {
        msg += `\n\nDid you mean one of these?\n${result.similarFiles.map((s) => `  - ${s}`).join("\n")}`;
      }
      return { content: [{ type: "text", text: msg }], isError: true };
    }
    const body = (await store.getDocumentBody(result.filepath, { fromLine: parsedFromLine, maxLines: parsedMaxLines })) ?? "";
    let text = body;
    if (lineNumbers) text = addLineNumbers(text, parsedFromLine || 1);
    if (result.context) text = `<!-- Context: ${result.context} -->\n\n` + text;
    return { content: [{ type: "resource", resource: {
      uri: `qmd://${encodeQmdPath(result.displayPath)}`, name: result.displayPath,
      title: result.title, mimeType: "text/markdown", text,
    } }] };
  });

  server.registerTool("multi_get", {
    title: "Multi-Get Documents",
    description: "Retrieve multiple documents by glob pattern or comma-separated list. Results outside permitted collections are filtered out.",
    annotations: { readOnlyHint: true, openWorldHint: false },
    inputSchema: {
      pattern: z.string().describe("Glob pattern or comma-separated file paths"),
      maxLines: z.number().optional(),
      maxBytes: z.number().optional().default(10240),
      lineNumbers: z.boolean().optional().default(true),
    },
  }, async ({ pattern, maxLines, maxBytes, lineNumbers }) => {
    const { docs, errors } = await store.multiGet(pattern, { includeBody: true, maxBytes: maxBytes || DEFAULT_MULTI_GET_MAX_BYTES });
    const allowedDocs = docs.filter((r) =>
      r.skipped ? docAllowed(undefined, r.doc.filepath) : docAllowed(r.doc.collectionName, r.doc.filepath)); // ENFORCE
    if (allowedDocs.length === 0 && errors.length === 0) {
      return { content: [{ type: "text", text: `No files matched pattern (in permitted collections): ${pattern}` }], isError: true };
    }
    const content = [];
    if (errors.length > 0) content.push({ type: "text", text: `Errors:\n${errors.join("\n")}` });
    for (const result of allowedDocs) {
      if (result.skipped) {
        content.push({ type: "text", text: `[SKIPPED: ${result.doc.displayPath} - ${result.skipReason}. Use 'get' with file="${result.doc.displayPath}".]` });
        continue;
      }
      let text = result.doc.body || "";
      if (maxLines !== undefined) {
        const lines = text.split("\n");
        text = lines.slice(0, maxLines).join("\n");
        if (lines.length > maxLines) text += `\n\n[... truncated ${lines.length - maxLines} more lines]`;
      }
      if (lineNumbers) text = addLineNumbers(text);
      if (result.doc.context) text = `<!-- Context: ${result.doc.context} -->\n\n` + text;
      content.push({ type: "resource", resource: {
        uri: `qmd://${encodeQmdPath(result.doc.displayPath)}`, name: result.doc.displayPath,
        title: result.doc.title, mimeType: "text/markdown", text,
      } });
    }
    return { content };
  });

  server.registerTool("status", {
    title: "Index Status",
    description: "Show index health for THIS agent's permitted collections only.",
    annotations: { readOnlyHint: true, openWorldHint: false },
    inputSchema: {},
  }, async () => {
    const cols = (await store.listCollections()).filter((c) => allowedSet.has(c.name)); // ENFORCE
    const total = cols.reduce((n, c) => n + (c.doc_count || 0), 0);
    const summary = [
      "QMD Index Status (scoped):",
      `  Permitted collections: ${cols.length}`,
      `  Total documents (permitted): ${total}`,
    ];
    for (const c of cols) summary.push(`    - ${c.name}: ${c.pwd} (${c.doc_count} docs)`);
    return { content: [{ type: "text", text: summary.join("\n") }], structuredContent: { collections: cols, totalDocuments: total } };
  });

  return server;
}

// --- transport selection -----------------------------------------------------
const httpPort = process.env.QMD_GW_HTTP_PORT ? parseInt(process.env.QMD_GW_HTTP_PORT, 10) : 0;

if (httpPort) {
  // Streamable HTTP — one McpServer+transport per session (MCP spec). Store shared.
  const sessions = new Map();
  async function createSession() {
    const transport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      enableJsonResponse: true,
      onsessioninitialized: (id) => sessions.set(id, transport),
    });
    const server = buildServer();
    await server.connect(transport);
    transport.onclose = () => { if (transport.sessionId) sessions.delete(transport.sessionId); };
    return transport;
  }
  async function collectBody(req) {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    return Buffer.concat(chunks).toString();
  }
  const httpServer = createServer(async (nodeReq, nodeRes) => {
    const pathname = (nodeReq.url || "/").split("?")[0];
    try {
      if (pathname === "/health" && nodeReq.method === "GET") {
        nodeRes.writeHead(200, { "Content-Type": "application/json" });
        nodeRes.end(JSON.stringify({ status: "ok", allowed: ALLOWED }));
        return;
      }
      if (pathname === "/mcp") {
        const headers = {};
        for (const [k, v] of Object.entries(nodeReq.headers)) if (typeof v === "string") headers[k] = v;
        const sessionId = headers["mcp-session-id"];
        let rawBody, body;
        if (nodeReq.method === "POST") {
          rawBody = await collectBody(nodeReq);
          body = JSON.parse(rawBody);
        }
        let transport;
        if (sessionId) {
          transport = sessions.get(sessionId);
          if (!transport) {
            nodeRes.writeHead(404, { "Content-Type": "application/json" });
            nodeRes.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32001, message: "Session not found" }, id: body?.id ?? null }));
            return;
          }
        } else if (nodeReq.method === "POST" && isInitializeRequest(body)) {
          transport = await createSession();
        } else {
          nodeRes.writeHead(400, { "Content-Type": "application/json" });
          nodeRes.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "Bad Request: Missing session ID" }, id: body?.id ?? null }));
          return;
        }
        const url = `http://localhost:${httpPort}${pathname}`;
        const request = new Request(url, {
          method: nodeReq.method,
          headers,
          ...(nodeReq.method === "POST" ? { body: rawBody } : {}),
        });
        const response = await transport.handleRequest(request, nodeReq.method === "POST" ? { parsedBody: body } : undefined);
        nodeRes.writeHead(response.status, Object.fromEntries(response.headers));
        nodeRes.end(Buffer.from(await response.arrayBuffer()));
        return;
      }
      nodeRes.writeHead(404, { "Content-Type": "application/json" });
      nodeRes.end(JSON.stringify({ error: "not found" }));
    } catch (e) {
      nodeRes.writeHead(500, { "Content-Type": "application/json" });
      nodeRes.end(JSON.stringify({ error: String(e?.message || e) }));
    }
  });
  // Bind host: 127.0.0.1 by default (safe for host/launchd use). In the qmd container
  // set QMD_GW_HTTP_HOST=0.0.0.0 so the agent container can reach it over the compose
  // network (the port is NOT published to the host there — only the in-network agent sees it).
  const httpHost = process.env.QMD_GW_HTTP_HOST || "127.0.0.1";
  httpServer.listen(httpPort, httpHost, () => {
    console.error(`qmd-scoped gateway (HTTP) on ${httpHost}:${httpPort} — allowlist: [${ALLOWED.join(", ")}], db: ${dbPath}`);
  });
} else {
  const transport = new StdioServerTransport();
  await buildServer().connect(transport);
  console.error(`qmd-scoped gateway (stdio) up — allowlist: [${ALLOWED.join(", ")}], db: ${dbPath}`);
}
