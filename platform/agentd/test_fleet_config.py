"""Hermetic tests for fleet_config — no docker, no network. Run: python3 test_fleet_config.py"""
import os, tempfile, pathlib, sys
os.environ["ENCLAVE_FLEET_AUDIT"] = tempfile.mktemp(suffix="-audit.log")
import fleet_config as fc

P = 0; F = 0
def ok(cond, msg):
    global P, F
    if cond: P += 1
    else: F += 1; print(f"  FAIL: {msg}")

def fresh(env_text):
    d = tempfile.mkdtemp()
    home = pathlib.Path(d) / "home"; home.mkdir()
    (home / "agent.env").write_text(env_text)
    return home

BASE = "# header comment\nAGENT_ID=demo\nBRAIN=claude\nMODEL=claude-opus-4-8\nINTERVAL_SECONDS=10800\nSUPERVISE=off\n"

# read_config
h = fresh(BASE)
cfg = fc.read_config(h)
ok(cfg["env"]["BRAIN"] == "claude", "read BRAIN")
ok(cfg["env"]["AGENT_ID"] == "demo", "read AGENT_ID")

# patch preserves comments + order, updates in place
diff = fc.patch_agent_env(h, {"BRAIN": "local"}, "demo")
txt = (h / "agent.env").read_text()
ok(txt.startswith("# header comment"), "comment preserved")
ok("BRAIN=local" in txt and "BRAIN=claude" not in txt, "BRAIN updated in place")
ok(txt.index("AGENT_ID") < txt.index("BRAIN"), "key order preserved")
ok(diff == [("BRAIN", "claude", "local")], f"diff correct: {diff}")

# new key appended
fc.patch_agent_env(h, {"ROUTER": "on"}, "demo")
ok((h / "agent.env").read_text().rstrip().endswith("ROUTER=on"), "new key appended")

# no-op when unchanged
ok(fc.patch_agent_env(h, {"BRAIN": "local"}, "demo") == [], "no-op on identical value")

# history snapshot written
hist = list((h / "state" / "config-history").glob("*.env"))
ok(len(hist) >= 1, "history snapshot created")
ok(any("BRAIN=claude" in f.read_text() for f in hist), "history holds prior value (no collision loss)")

# allowlist rejects identity keys
try:
    fc.patch_agent_env(h, {"AGENT_ID": "hacked"}, "demo"); ok(False, "should reject AGENT_ID")
except ValueError: ok(True, "rejects non-allowed key")
try:
    fc.patch_agent_env(h, {"SECRETS": "anthropic.env"}, "demo"); ok(False, "should reject SECRETS")
except ValueError: ok(True, "rejects SECRETS")

# brain validation
try:
    fc.set_brain(h, "gpt4", agent="demo"); ok(False, "should reject bad brain")
except ValueError: ok(True, "rejects invalid brain")
fc.set_brain(h, "optimize", "claude-sonnet-4-6", "demo")
e = fc.read_config(h)["env"]
ok(e["BRAIN"] == "optimize" and e["MODEL"] == "claude-sonnet-4-6", "set_brain applies brain+model")

# mode mapping
fc.set_mode(h, "autonomous", agent="demo")
ok(fc.read_config(h)["env"]["SUPERVISE"] == "auto", "autonomous -> SUPERVISE=auto")
fc.set_mode(h, "chat", agent="demo")
ok(fc.read_config(h)["env"]["SUPERVISE"] == "off", "chat -> SUPERVISE=off")
fc.set_mode(h, "scheduled", 3600, "demo")
e = fc.read_config(h)["env"]
ok(e["SUPERVISE"] == "off" and e["INTERVAL_SECONDS"] == "3600", "scheduled -> off + interval")
try:
    fc.set_mode(h, "bogus", agent="demo"); ok(False, "should reject bad mode")
except ValueError: ok(True, "rejects invalid mode")
try:
    fc.set_mode(h, "scheduled", "abc", "demo"); ok(False, "should reject non-int interval")
except ValueError: ok(True, "rejects non-int interval")

# presets
h2 = fresh(BASE)
fc.apply_preset(h2, "claude-managed", "demo")
e = fc.read_config(h2)["env"]
ok(e["BRAIN"] == "claude" and e["DELEGATION_ENFORCE"] == "on" and e["ROUTER"] == "on", "claude-managed preset")
h3 = fresh(BASE)
fc.apply_preset(h3, "autonomous-local-cheap", "demo")
ok(fc.read_config(h3)["env"]["BRAIN"] == "local", "autonomous-local-cheap preset")
try:
    fc.apply_preset(h3, "nope", "demo"); ok(False, "should reject unknown preset")
except ValueError: ok(True, "rejects unknown preset")

# ── dual-homed .env sync (the bug the operator caught) ──
h4 = fresh(BASE)
(h4.parent / ".env").write_text("AGENT_ID=demo\nBRAIN=claude\nMODEL=claude-opus-4-8\nWEB_CHAT_BIND=127.0.0.1:8891\n")
cfg = fc.read_config(h4)
ok(cfg["env"]["BRAIN"] == "claude", "merged read sees BRAIN")
ok("AGENT_ID" in cfg["env"] and "AGENT_ID" not in cfg["editable"], "identity key present but NOT editable")
ok("BRAIN" in cfg["editable"], "BRAIN is editable")
# .env wins on read (runtime-authoritative): make them differ, expect .env value
(h4 / "agent.env").write_text(BASE.replace("BRAIN=claude", "BRAIN=local"))
ok(fc.read_config(h4)["env"]["BRAIN"] == "claude", ".env value wins over agent.env on read")
# set_brain must sync BOTH files
fc.set_brain(h4, "optimize", agent="demo")
ok(fc.parse_env((h4 / "agent.env").read_text())["BRAIN"] == "optimize", "agent.env BRAIN updated")
ok(fc.parse_env((h4.parent / ".env").read_text())["BRAIN"] == "optimize", ".env BRAIN synced (dual-homed)")
ok("WEB_CHAT_BIND" in fc.parse_env((h4.parent / ".env").read_text()), ".env other keys preserved")
# a key NOT already in .env must NOT be added to .env (only agent.env)
fc.patch_agent_env(h4, {"ROUTER": "on"}, "demo")
ok("ROUTER" not in fc.parse_env((h4.parent / ".env").read_text()), "non-dual-homed key not added to .env")
ok(fc.parse_env((h4 / "agent.env").read_text())["ROUTER"] == "on", "non-dual-homed key written to agent.env")
# history snapshots both files
hist4 = list((h4 / "state" / "config-history").glob("*"))
ok(any(f.name.endswith(".dotenv") for f in hist4), "history snapshots .env too")

# audit log written
ok(pathlib.Path(os.environ["ENCLAVE_FLEET_AUDIT"]).exists(), "audit log written")

print(f"\n{'OK' if F == 0 else 'FAILED'} {P} passed, {F} failed")
sys.exit(1 if F else 0)
