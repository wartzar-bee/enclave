# Enclave chat — feature backlog (distilled, 2026-06-17)

Mined from the community Claude-Code web UIs (CloudCLI/`claudecodeui`, Claudeck, AI Hub, claude-code-webui)
to avoid reinventing — **distilled, not adopted**. Those are full Node/React/Vue apps with npm deps; they
violate Enclave's offline + zero-dep + single-baked-file + per-agent-isolation + must-vet model, so we
cherry-pick *ideas* and build minimal vanilla versions (same rule that gave us our own comic/voice tooling).

Sources: github.com/siteboon/claudecodeui · hamedfarag.dev/posts/claudeck · nimbalyst.com/blog/best-claude-code-gui-tools-2026

## ✅ Already shipped
Multi-conversation sidebar (new/search/delete/star) · continuous resumable sessions (survive rebuilds) ·
slash-command menu (skills + /clear/help, substring search) · stop button · markdown→HTML (tables/lists) ·
file downloads (/agent/outputs) · image upload · voice in/out · live model switch · auto topic titles ·
token-gated auth · brain-agnostic (claude/api/local).

## 🟢 Worth building (fits the zero-dep/hardened model)
1. **Usage/cost line** — tokens + $ per thread. We already capture it (`claude_usage.py`/`usage.py`); just
   surface it (a `/cost` command + a small footer). Low effort, high "is this expensive?" value.
2. **Browser notification on turn completion** — turns run up to ~150s; `Notification` API (zero-dep) pings
   when a reply lands if the tab's backgrounded. Small.
3. **Mobile/responsive layout** — sidebar → slide-over on narrow screens; composer/table reflow. CSS only.
4. **Keyboard shortcuts** — ⌘K focus+slash, ⌘N new chat, Esc = stop/close menu. Small polish.
5. **Conversation export** — download a thread as `.md` (trivial given the /download infra).
6. **Regenerate / edit-last-message** — re-run or amend the previous turn. Moderate.

## 🟡 Bigger lifts (worth it, but real work)
7. **Streaming replies** — token-by-token via `claude --output-format stream-json` instead of poll-then-dots.
   Best UX upgrade; requires reworking the chat plane from poll → SSE/stream. Plan separately.
8. **@file mentions** — `@` autocompletes files in `/work` into the message (works in `-p`); pairs with the
   slash menu. Moderate.

## 🔴 Skip — conflict with the hardened ops-agent model
- **Integrated terminal in the UI** — a shell in the browser is a large new attack surface; the guard +
  scoped container exist precisely to avoid this. No.
- **File explorer / live code editing / IDE panes** — the chat is Q&A + deliverables, not an IDE; the agent
  works in `/work` and the operator reviews. Out of scope.
- **Git stage/commit/branch UI** — the agent can't `git` (guard-blocked); the operator owns commits.
- **Diff viewer / per-file accept-reject, Kanban multi-parallel sessions, visual editors (mockups/diagrams/
  spreadsheets)** — dev-workflow features for coding agents; not relevant to a read-only ops/support agent.

**Stance:** these are options, not commitments. Build #1–#5 as small wins when wanted; treat #7 (streaming)
as the one genuinely large upgrade. Everything in 🔴 is a deliberate non-goal for the security posture.
