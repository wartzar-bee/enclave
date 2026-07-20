#!/usr/bin/env python3
"""
agentloop.py — the PERSISTENT, EVENT-DRIVEN runtime for a self-contained agent (P0).

Replaces the image's old time-driven loop:

    while true; do runtime.sh; sleep ${INTERVAL_SECONDS}; done   # waits up to 3h, deaf

The container was ALREADY a persistent peer; the only defect was that the loop slept on a
fixed timer with nothing listening, so a directive couldn't reach a running agent for up to
one INTERVAL. This loop waits on {heartbeat timer OR a new directive} and, on wake, runs the
SAME baked `runtime.sh` for one cap-guarded `claude -p` turn. No warm session, no SDK, no
broker — cap-discipline, stale-lock, deadman all stay inside runtime.sh and are reused as-is.

Directive sources (checked every POLL_SECONDS, cheap):
  • inbox.md mtime bump — `agentctl send`/`agentctl msg` writes the file → wake within seconds
  • comms bridge (P1, optional) — when COMMS_URL is set, an outbound poll of the host comms
    bridge (host.docker.internal:18193), the same outbound pattern as qmd/voice/gcloud.
With neither, behaviour degrades to a pure INTERVAL heartbeat — i.e. exactly today's cadence.

Queue, don't drop: if a turn is deferred (runtime.sh exits 75 on cap-guard / lock skip), the
inbox/comms baselines are NOT advanced and the loop backs off CAP_RETRY_SECONDS, then retries
— the directive survives the spent 5h block instead of being marked seen.

  python3 agentloop.py <agent-dir>        # <agent-dir> defaults to $AGENT_DIR or /agent
Env: INTERVAL_SECONDS(10800) POLL_SECONDS(5) CAP_RETRY_SECONDS(600) INITIAL_TICK(1)
     COMMS_URL(unset) AGENT_ID RUNTIME(<sibling runtime.sh>)
"""
import os, sys, time, json, subprocess, pathlib, urllib.request, urllib.error

HERE = pathlib.Path(__file__).resolve().parent
SKIP_RC = 75                                   # runtime.sh: cap-guard / lock skip (deferred, not done)


def due(now, next_heartbeat, inbox_changed, comms_pending, defer_until):
    """PURE wake decision (unit-tested). A deferral window blocks ALL wakes (a capped/locked
    agent can run nothing regardless of source); otherwise directives beat the heartbeat."""
    if now < defer_until:
        return None
    if comms_pending:
        return "comms"
    if inbox_changed:
        return "inbox"
    if now >= next_heartbeat:
        return "heartbeat"
    return None


def _mtime(p):
    try: return p.stat().st_mtime
    except OSError: return None


def _comms_http(url, token, method, path, params=None, body=None, timeout=4):
    """Best-effort token-authed call to the host comms bridge (P1, host.docker.internal:18193).
    Outbound only — same pattern as the qmd/voice/gcloud bridges. Any error → None so the loop
    never wedges on a missing/slow bridge."""
    if not url:
        return None
    try:
        q = f"{url.rstrip('/')}{path}"
        if params:
            from urllib.parse import urlencode
            q += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(q, data=data, method=method)
        req.add_header("X-Comms-Token", token or "")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Loop:
    def __init__(self, agent_dir):
        self.dir = pathlib.Path(agent_dir).resolve()
        self.runtime = pathlib.Path(os.environ.get("RUNTIME", HERE / "runtime.sh"))
        self.interval = int(os.environ.get("INTERVAL_SECONDS", "10800"))
        self.poll = max(1, int(os.environ.get("POLL_SECONDS", "5")))
        self.cap_retry = int(os.environ.get("CAP_RETRY_SECONDS", "600"))
        self.comms_url = os.environ.get("COMMS_URL", "").strip()
        self.agent_id = os.environ.get("AGENT_ID", self.dir.name)
        self.comms_token = self._read_comms_token()
        self.inbox = self.dir / "inbox.md"
        self.logf = self.dir / "logs" / "runner.log"
        self.inbox_baseline = _mtime(self.inbox)
        self.next_heartbeat = 0.0                  # 0 ⇒ first heartbeat is due immediately
        self.defer_until = 0.0
        # Continuous mode: how fast to re-fire when the tick says it has more work.
        # NOTE (token-economy, 2026-06-13): defaults raised from 60s/30s. Back-to-back 60s ticks ×
        # several pods × deep (40-min) ticks on opus = the "weekly cap in a day" burn. A pod with a
        # backlog now paces at 15min (still continuous, just not hot), with a 5-min HARD floor so even
        # an explicit fast 'continue' can't hot-spin. Tune per agent via agent.env / fleet.env; for a
        # genuine budgeted build-sprint set CONTINUOUS_COOLDOWN lower deliberately.
        self.cont_cooldown = int(os.environ.get("CONTINUOUS_COOLDOWN", "900"))  # 'continue'/backlog → next tick (15m)
        self.min_cooldown = int(os.environ.get("MIN_COOLDOWN", "300"))          # hard floor (no hot spin) — 5m
        self.default_pace = int(os.environ.get("DEFAULT_PACE", "600"))          # tick wrote no status
        # Co-located OFF-OPUS supervisor (self-contained pod, 2026-06-13): a BRAIN=local worker needs a
        # supervisor (cheap external planner + deterministic verify-gate) to plan bounded tasks, keep the
        # queue full, and escalate stuck items. It steers the worker purely through shared agent-dir files
        # (work.json / phase-goal.txt / escalations.log) and has NO host-only dependency — so it runs as a
        # sibling process IN this container, not as a host script. SUPERVISE: auto (on iff BRAIN=local)|on|off.
        self.brain = os.environ.get("BRAIN", "").strip().lower()
        self.supervise = os.environ.get("SUPERVISE", "auto").strip().lower()
        self.sup_proc = None

    def log(self, msg):
        line = f"{time.strftime('%FT%TZ', time.gmtime())} — [{self.agent_id}] loop: {msg}"
        print(line, flush=True)                    # → docker logs
        try:
            self.logf.parent.mkdir(parents=True, exist_ok=True)
            with self.logf.open("a") as f: f.write(line + "\n")
        except OSError:
            pass

    def _ensure_supervisor(self):
        """Keep an OFF-OPUS supervisor running alongside the worker, IN this container, for
        BRAIN=local pods. Self-healing: (re)spawn if absent or it has exited. Cheap to call each
        poll (just a poll() liveness check when already alive)."""
        on = self.supervise == "on" or (self.supervise == "auto" and self.brain == "local")
        if not on:
            return
        if self.sup_proc is not None and self.sup_proc.poll() is None:
            return                                     # already alive
        sup = HERE / "supervisor.py"
        if not sup.exists():
            return
        try:
            respawn = self.sup_proc is not None
            self.sup_proc = subprocess.Popen([sys.executable, str(sup), str(self.dir)], env=dict(os.environ))
            self.log(f"supervisor (off-opus) {'re' if respawn else ''}spawned in-container pid={self.sup_proc.pid}")
        except OSError as e:
            self.log(f"supervisor spawn failed: {e}")

    def _start_supervisor_watchdog(self):
        """Respawn the supervisor if it dies, INDEPENDENT of the synchronous (≤40-min) worker tick —
        so a crash mid-tick still heals within ~30s. Daemon thread owns sup_proc after the initial
        spawn, so it never races the main loop (which no longer touches it)."""
        on = self.supervise == "on" or (self.supervise == "auto" and self.brain == "local")
        if not on:
            return
        import threading
        def _wd():
            while True:
                time.sleep(30)
                self._ensure_supervisor()
        threading.Thread(target=_wd, daemon=True, name="sup-watchdog").start()

    def _start_chat_responder(self):
        """Spawn the REAL-TIME chat responder in a daemon thread. It watches state/chat-inbox.jsonl and
        answers operator chat from live state via a cheap model — CONCURRENT with the work tick, so a
        long (≤40-min) task never blocks a reply. Separate plane from the work inbox. Fail-open: if it
        can't start, the agent still works (just no live chat). Disable with CHAT_RESPONDER=off."""
        if os.environ.get("CHAT_RESPONDER", "on") == "off":
            return
        import threading
        try:
            sys.path.insert(0, str(HERE))
            import chat_responder
        except Exception as e:
            self.log(f"chat responder not started: {e}")
            return
        threading.Thread(target=chat_responder.chat_loop, args=(str(self.dir),),
                         kwargs={"log": self.log}, daemon=True, name="chat-responder").start()

    def _run_boot_hook(self):
        """Run an optional per-pod boot command once at container start (env BOOT_HOOK) — e.g. bring the
        pod's playtest servers up so a container restart SELF-HEALS them, independent of the tick cadence.
        Fire-and-forget + fail-open: a missing/broken hook never blocks the loop. Unset by default."""
        cmd = os.environ.get("BOOT_HOOK", "").strip()
        if not cmd:
            return
        try:
            subprocess.Popen(["sh", "-c", cmd], cwd=str(self.dir),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log(f"boot hook launched: {cmd[:80]}")
        except Exception as e:
            self.log(f"boot hook failed: {e}")

    def run_tick(self, reason):
        """One synchronous cap-guarded turn via the baked runtime.sh. Synchronous ⇒ the loop
        serialises with the tick (and runtime.sh's own lock), so turns never overlap."""
        self.log(f"wake ({reason}) → tick")
        try:
            env = dict(os.environ, TICK_REASON=reason)     # the router tiers the model by trigger
            rc = subprocess.call(["bash", str(self.runtime), str(self.dir)], env=env)
        except OSError as e:
            self.log(f"tick spawn error: {e}"); rc = 1
        return rc

    def _last_model(self):
        """The model the last tick actually ran on (router decision), for the emit event."""
        try:
            for ln in reversed((self.dir / "logs" / "runner.log").read_text().splitlines()):
                if "tick start (model=" in ln:
                    return ln.split("model=", 1)[1].split(",", 1)[0]
        except OSError:
            pass
        return ""

    def _read_comms_token(self):
        t = os.environ.get("COMMS_TOKEN", "").strip()
        if t:
            return t
        f = pathlib.Path(os.environ.get("COMMS_TOKEN_FILE", "/workspace/.secrets/comms-bridge.env"))
        try:
            for ln in f.read_text().splitlines():
                if ln.startswith("COMMS_BRIDGE_TOKEN="):
                    return ln.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def comms_pending(self):
        resp = _comms_http(self.comms_url, self.comms_token, "GET", "/pending", params={"agent": self.agent_id})
        return bool((resp or {}).get("pending"))

    def drain_comms(self):
        """Pull queued directives off the bridge into inbox.md so the upcoming tick reads them
        via its normal inbox path — the agent's mental model stays 'always read inbox.md'."""
        if not self.comms_url:
            return 0
        resp = _comms_http(self.comms_url, self.comms_token, "GET", "/inbox", params={"agent": self.agent_id})
        ds = (resp or {}).get("directives") or []
        if not ds:
            return 0
        try:
            with self.inbox.open("a") as f:
                for d in ds:
                    f.write(f"\n- [ ] {time.strftime('%FT%TZ', time.gmtime())} via comms ({d.get('frm','master')}): {d.get('text','')}\n")
        except OSError:
            return 0
        self.log(f"drained {len(ds)} comms directive(s) → inbox.md")
        return len(ds)

    def emit_tick(self, reason, rc):
        """Push a live event so `agentctl attach` sees the turn (reason, rc, last activity line)."""
        if not self.comms_url:
            return
        last = ""
        try:
            al = self.dir / "state" / "activity.log"
            if al.exists():
                lines = al.read_text().splitlines()
                last = lines[-1] if lines else ""
        except OSError:
            pass
        _comms_http(self.comms_url, self.comms_token, "POST", "/emit",
                    body={"agent": self.agent_id, "type": "tick",
                          "data": {"reason": reason, "rc": rc, "deferred": rc == SKIP_RC,
                                   "model": self._last_model(), "last": last[-300:]}})

    def tick_cycle(self, reason):
        """One full cycle: drain bridge directives → run the cap-guarded turn → emit the result."""
        self.drain_comms()
        rc = self.run_tick(reason)
        self.emit_tick(reason, rc)
        self._after(rc)

    def run(self):
        self.log(f"start (interval={self.interval}s poll={self.poll}s comms={'on' if self.comms_url else 'off'})")
        self._ensure_supervisor()                  # co-located off-opus supervisor (BRAIN=local)
        self._start_supervisor_watchdog()          # tick-independent self-heal (the loop blocks during a tick)
        self._start_chat_responder()               # REAL-TIME chat plane (concurrent; never blocked by a work tick)
        self._run_boot_hook()                      # optional per-pod boot cmd (e.g. start playtest servers) — self-heals on restart
        if os.environ.get("INITIAL_TICK", "1") != "0":
            self.tick_cycle("startup")             # default: tick now (matches the old while/sleep loop)
        else:
            self.next_heartbeat = time.time() + self.interval   # honor INITIAL_TICK=0: no immediate tick
        while True:
            time.sleep(self.poll)
            now = time.time()
            ibm = _mtime(self.inbox)
            inbox_changed = (ibm is not None and self.inbox_baseline is not None and ibm > self.inbox_baseline)
            reason = due(now, self.next_heartbeat, inbox_changed, self.comms_pending(), self.defer_until)
            if reason:
                self.tick_cycle(reason)

    def _skip_reason(self):
        """Why runtime.sh returned SKIP_RC. It exits 75 for several unrelated reasons — paused,
        spend cap, session cap, a live lock from a previous tick — and the loop logged all of them
        as "cap/lock". A pod paused by a deliberate venture decision therefore read for 15 days as
        though it were throttled by a budget guard (stoneforge, 2026-07-04 → 07-20). The specific
        reason is already known one layer down; surface it instead of flattening it.
        """
        try:
            if (pathlib.Path(self.agent_dir) / "state" / "paused").exists():
                return "paused"
        except Exception:
            pass
        return "cap/lock"

    def _after(self, rc):
        """Advance baselines on a DONE turn; on a deferred turn (SKIP_RC) hold baselines and
        back off so the directive is retried, not lost."""
        now = time.time()
        if rc == SKIP_RC:
            self.defer_until = now + self.cap_retry
            self.log(f"tick deferred ({self._skip_reason()}) — retry in {self.cap_retry}s")
            return
        self.inbox_baseline = _mtime(self.inbox)
        # CONTINUOUS MODE: agents work back-to-back, not tick-then-sleep-3h. The tick writes
        # state/tick-status.json {status: "continue"|"idle", cooldown_s?, waiting_on?}. continue →
        # re-fire after a short cooldown (uninterrupted work); idle → fall to the slow heartbeat but
        # still wake instantly on events (comms/inbox). No signal → a safe middle pace. The cap-guard
        # in runtime.sh throttles the burn, so continuous != runaway.
        st = self._read_tick_status()
        # Honor the agent's context-CLEAR signal HERE. _read_tick_status consumes+deletes tick-status.json
        # (one-shot), so runtime.sh's own session-clear check can NEVER see it on the next tick — the agent's
        # self-clear was silently lost and the warm session grew until a cost/occupancy net tripped (the
        # warm-resume cost deadlock). Drop the pinned session id now so the next tick cold-starts a fresh
        # (cheap) session from handoff.md — restoring lean per-package clears.
        if str(st.get("session", "")).strip().lower() in ("clear", "fresh", "reset", "new"):
            try:
                (self.dir / "state" / "work-session.id").unlink()
                self.log("agent signalled session CLEAR → dropped warm-session id (fresh session next tick)")
            except FileNotFoundError:
                pass
            except Exception as e:
                self.log(f"session CLEAR signal: could not drop session id ({e})")
        if st.get("status") == "blocked":
            # BLOCKED (2026-07-04 enclave review fix #7): the agent is waiting on something EXTERNAL
            # (operator answer / dead key / broken bridge) — park at the slow heartbeat instead of
            # re-firing paid continuous ticks that can only re-log "still blocked" (stoneforge burned
            # 8 back-to-back Opus WAIT ticks polling a dead image key). Wake-on-inbox/comms still
            # applies, so an operator reply resumes it within seconds; the INTERVAL heartbeat
            # self-checks the blocker a couple of times an hour. Anti-gaming: a real block names its
            # dependency (waiting_on, or a state/blockers/ file) — the marker feeds the monitor/console
            # so a long-standing block is VISIBLE, not an excuse.
            why = str(st.get("waiting_on") or "").strip()
            bdir = self.dir / "state" / "blockers"
            try:
                has_file = bdir.is_dir() and any(bdir.iterdir())
            except OSError:
                has_file = False
            self.next_heartbeat = now + self.interval
            if why or has_file:
                self._write_blocked_marker(now, why or "see state/blockers/")
                self.log(f"BLOCKED ({why or 'see state/blockers/'}) → parked at heartbeat {self.interval}s (wakes on inbox/comms)")
            else:
                self.log(f"blocked status without waiting_on or a state/blockers/ file — parked at heartbeat {self.interval}s; NAME the dependency next time")
        elif st.get("status") == "idle":
            self._clear_blocked_marker()
            self.next_heartbeat = now + self.interval
            self.log(f"idle ({st.get('waiting_on','-')}) → heartbeat {self.interval}s (wakes on events)")
        elif st.get("status") == "continue":
            self._clear_blocked_marker()
            cd = max(self.min_cooldown, int(st.get("cooldown_s") or self.cont_cooldown))
            self.next_heartbeat = now + cd
            self.log(f"continue → next tick in {cd}s")
        else:
            # No explicit signal — default to CONTINUOUS while there's open work (so agents work
            # back-to-back on their backlog without having to remember to write tick-status), and
            # idle only when the queue is genuinely empty (then wake on events). This is the robust
            # default: continuous work doesn't depend on agent discipline.
            if self._has_open_work():
                self._clear_blocked_marker()
                self.next_heartbeat = now + self.cont_cooldown
                self.log(f"no tick-status + open work → continue in {self.cont_cooldown}s")
            else:
                self.next_heartbeat = now + self.interval
                self.log("no tick-status + empty queue → idle heartbeat (wakes on events)")
        self.defer_until = 0.0

    def _has_open_work(self):
        """True if the agent's work.json has any open/doing item — used to keep the loop continuous
        by default while there's a backlog."""
        try:
            w = json.loads((self.dir / "work.json").read_text())
            return any(i.get("status") not in ("done", "dropped") for i in w)
        except Exception:
            return False

    def _write_blocked_marker(self, now, why):
        """state/.blocked {since, waiting_on} — 'since' survives repeat blocked ticks so the console/
        monitor can see HOW LONG the agent has been parked on the same dependency."""
        bm = self.dir / "state" / ".blocked"
        try:
            since = int(now)
            try:
                prev = json.loads(bm.read_text())
                since = int(prev.get("since") or since)
            except Exception:
                pass
            bm.parent.mkdir(parents=True, exist_ok=True)
            bm.write_text(json.dumps({"since": since, "waiting_on": why}))
        except OSError:
            pass

    def _clear_blocked_marker(self):
        try:
            (self.dir / "state" / ".blocked").unlink()
        except OSError:
            pass

    def _read_tick_status(self):
        """Read state/tick-status.json the tick wrote to declare continue vs idle. Best-effort:
        missing/garbled => {} (caller applies the safe default pace). One-shot — consumed each
        cycle so a stale 'continue' can't pin the loop hot forever."""
        f = self.dir / "state" / "tick-status.json"
        try:
            d = json.loads(f.read_text())
            f.unlink()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}


def main():
    agent_dir = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent")
    Loop(agent_dir).run()


if __name__ == "__main__":
    main()
