#!/usr/bin/env node
// codegraph_gateway.mjs — Enclave HTTP MCP bridge over codegraph (the SHARED code-memory mode).
//
// codegraph's own MCP server is STDIO-only (`codegraph serve --mcp`), so it can't be shared over a
// network the way qmd's HTTP gateway can. This bridge fixes that: ONE process serves the codegraph
// code-knowledge of a mounted corpus to MANY agents over Streamable HTTP — the true qmd-style
// "shared" mode (agents need NOTHING baked in; they just point .mcp.json at http://codegraph:PORT/mcp).
//
// It reuses the SAME proven Streamable-HTTP transport as qmd_gateway.mjs; the tools exec the
// `codegraph` CLI over the corpus (each subcommand mirrors the matching MCP tool's output). The
// corpus mount IS the scope (no cross-corpus access) — codegraph holds no collections/allowlist.
//
// Env:
//   CODEGRAPH_CORPUS         (default /corpus) project path the index was built over.
//   CODEGRAPH_GW_HTTP_PORT   (default 18184) Streamable HTTP port. Unset → stdio.
//   CODEGRAPH_GW_HTTP_HOST   (default 127.0.0.1; the container sets 0.0.0.0 for in-network reach).
//   CODEGRAPH_BIN            (default "codegraph") the CLI to exec.
//   DO_NOT_TRACK=1 is baked in the image (telemetry off).
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";

const CORPUS = process.env.CODEGRAPH_CORPUS || "/corpus";
const BIN = process.env.CODEGRAPH_BIN || "codegraph";
const ANSI = /\x1b\[[0-9;]*m/g;

// Run a codegraph subcommand in the corpus dir; return clean stdout. execFile (no shell) — args
// are passed as an array, so a tool argument can never inject a second command.
function runCodegraph(args) {
  return new Promise((resolve) => {
    execFile(BIN, args, { cwd: CORPUS, timeout: 60000, maxBuffer: 8 * 1024 * 1024, env: { ...process.env, DO_NOT_TRACK: "1" } },
      (err, stdout, stderr) => {
        const out = ((stdout || "") + (stderr ? "\n" + stderr : "")).replace(ANSI, "").trim();
        if (err && !out) return resolve(`codegraph ${args[0]} error: ${err.message}`);
        resolve(out || "(no output)");
      });
  });
}

const instructions = [
  "codegraph code-knowledge for the mounted repository corpus (shared, read-only-intent).",
  "Tools: search (find symbols), explore (area: source + call paths), node (one symbol's source +",
  "caller/callee trail or a file), callers/callees (who calls / is called by), impact (what a change",
  "affects), files (project structure), status (index health). Use these instead of grep-looping code.",
].join("\n");

// One McpServer per HTTP session (matches qmd_gateway). Tools exec the CLI over the shared corpus.
function buildServer() {
  const server = new McpServer({ name: "codegraph-bridge", version: "1.0.0" }, { instructions });
  const ro = { readOnlyHint: true, openWorldHint: false };
  const text = (s) => ({ content: [{ type: "text", text: s }] });

  server.registerTool("codegraph_search", {
    title: "Search symbols", description: "Search for symbols across the codebase.", annotations: ro,
    inputSchema: { query: z.string().describe("symbol name or text to search for") },
  }, async ({ query }) => text(await runCodegraph(["query", query])));

  server.registerTool("codegraph_explore", {
    title: "Explore area", description: "Relevant symbols' source + call paths for a query, in one shot.", annotations: ro,
    inputSchema: { query: z.string().describe("what area/feature to explore") },
  }, async ({ query }) => text(await runCodegraph(["explore", query])));

  server.registerTool("codegraph_node", {
    title: "Node", description: "One symbol's source + caller/callee trail, or a file with line numbers + dependents.", annotations: ro,
    inputSchema: { name: z.string().describe("symbol name or file path") },
  }, async ({ name }) => text(await runCodegraph(["node", name])));

  server.registerTool("codegraph_callers", {
    title: "Callers", description: "All functions/methods that call a symbol.", annotations: ro,
    inputSchema: { symbol: z.string() },
  }, async ({ symbol }) => text(await runCodegraph(["callers", symbol])));

  server.registerTool("codegraph_callees", {
    title: "Callees", description: "All functions/methods a symbol calls.", annotations: ro,
    inputSchema: { symbol: z.string() },
  }, async ({ symbol }) => text(await runCodegraph(["callees", symbol])));

  server.registerTool("codegraph_impact", {
    title: "Impact", description: "What code is affected by changing a symbol.", annotations: ro,
    inputSchema: { symbol: z.string() },
  }, async ({ symbol }) => text(await runCodegraph(["impact", symbol])));

  server.registerTool("codegraph_files", {
    title: "Files", description: "Project file structure from the index.", annotations: ro,
    inputSchema: {},
  }, async () => text(await runCodegraph(["files"])));

  server.registerTool("codegraph_status", {
    title: "Status", description: "Index status and statistics.", annotations: ro,
    inputSchema: {},
  }, async () => text(await runCodegraph(["status"])));

  return server;
}

// --- transport selection (identical shape to qmd_gateway.mjs) -----------------
const httpPort = process.env.CODEGRAPH_GW_HTTP_PORT ? parseInt(process.env.CODEGRAPH_GW_HTTP_PORT, 10) : 0;

if (httpPort) {
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
        nodeRes.end(JSON.stringify({ status: "ok", corpus: CORPUS }));
        return;
      }
      if (pathname === "/mcp") {
        const headers = {};
        for (const [k, v] of Object.entries(nodeReq.headers)) if (typeof v === "string") headers[k] = v;
        const sessionId = headers["mcp-session-id"];
        let rawBody, body;
        if (nodeReq.method === "POST") { rawBody = await collectBody(nodeReq); body = JSON.parse(rawBody); }
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
        const request = new Request(url, { method: nodeReq.method, headers, ...(nodeReq.method === "POST" ? { body: rawBody } : {}) });
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
  const httpHost = process.env.CODEGRAPH_GW_HTTP_HOST || "127.0.0.1";
  httpServer.listen(httpPort, httpHost, () => {
    console.error(`codegraph bridge (HTTP) on ${httpHost}:${httpPort} — corpus: ${CORPUS}`);
  });
} else {
  const transport = new StdioServerTransport();
  await buildServer().connect(transport);
  console.error(`codegraph bridge (stdio) up — corpus: ${CORPUS}`);
}
