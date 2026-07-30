#!/usr/bin/env python3
"""Tests for catalog.py — the editable console catalog (seed, merge, CRUD, per-pool id validation).

Run: python3 test_catalog.py"""
import json, os, pathlib, tempfile
import catalog


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    assert cond, name


with tempfile.TemporaryDirectory() as d:
    os.environ["ENCLAVE_CONSOLE_CATALOG"] = str(pathlib.Path(d) / "cat.json")
    os.environ["ENCLAVE_FLEET_AUDIT"] = str(pathlib.Path(d) / "audit.log")

    # ── seed-on-first-read ──
    cat = catalog.load()
    check("seed created the store", (pathlib.Path(d) / "cat.json").exists())
    check("seed has current claude tier", "claude-fable-5" in cat["models"]["claude"]
          and "claude-sonnet-5" in cat["models"]["claude"])
    check("seed providers", set(cat["providers"]) >= {"nvidia", "openrouter"})
    check("seed presets", "claude-managed" in cat["presets"])

    # ── per-pool id-format validation (the claude-CLI-vs-slug trap) ──
    check("claude pool rejects slug", "error" in catalog.add_model("claude", "anthropic/claude-x"))
    check("api pool rejects bare id", "error" in catalog.add_model("api", "claude-x"))
    check("nvidia pool rejects bare id", "error" in catalog.add_model("nvidia", "some-model"))
    check("empty id rejected", "error" in catalog.add_model("claude", " "))

    # ── model CRUD persists ──
    r = catalog.add_model("claude", "claude-test-9")
    check("add claude model ok", r.get("ok") and "claude-test-9" in r["catalog"]["models"]["claude"])
    check("duplicate add rejected", "error" in catalog.add_model("claude", "claude-test-9"))
    r = catalog.add_model("nvidia", "foo/bar-7b")
    check("add provider model ok", r.get("ok") and "foo/bar-7b" in r["catalog"]["provider_models"]["nvidia"])
    check("persisted across load", "claude-test-9" in catalog.load()["models"]["claude"])
    r = catalog.remove_model("claude", "claude-test-9")
    check("remove model ok", r.get("ok") and "claude-test-9" not in r["catalog"]["models"]["claude"])
    check("remove unknown errors", "error" in catalog.remove_model("claude", "nope"))

    # ── provider CRUD ──
    r = catalog.upsert_provider("groq", {"base": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"})
    check("add provider ok", r.get("ok") and "groq" in r["catalog"]["providers"])
    check("provider gets empty model list", r["catalog"]["provider_models"]["groq"] == [])
    check("provider needs http base", "error" in catalog.upsert_provider("x", {"base": "ftp://x", "key_env": "K"}))
    r = catalog.remove_provider("groq")
    check("remove provider drops its models too", r.get("ok") and "groq" not in r["catalog"]["provider_models"])

    # ── preset CRUD ──
    r = catalog.upsert_preset("my-preset", {"BRAIN": "claude", "MODEL": "claude-fable-5"})
    check("add preset ok", r.get("ok") and "my-preset" in r["catalog"]["presets"])
    check("preset keys must be UPPER", "error" in catalog.upsert_preset("x", {"brain": "claude"}))
    check("catalog.presets() serves it", "my-preset" in catalog.presets())
    r = catalog.remove_preset("my-preset")
    check("remove preset ok", r.get("ok"))

    # ── merge: operator edits survive; new seed keys appear ──
    store = json.loads((pathlib.Path(d) / "cat.json").read_text())
    store["models"]["claude"] = ["claude-only-mine"]
    del store["providers"]          # simulate a store written before providers existed
    (pathlib.Path(d) / "cat.json").write_text(json.dumps(store))
    cat = catalog.load()
    check("stored edit wins", cat["models"]["claude"] == ["claude-only-mine"])
    check("missing section falls back to seed", "nvidia" in cat["providers"])

    # ── unreadable store never crashes ──
    (pathlib.Path(d) / "cat.json").write_text("{broken")
    check("corrupt store serves seed", "claude-opus-4-8" in catalog.load()["models"]["claude"])

    # ── mutations are audited ──
    audit = (pathlib.Path(d) / "audit.log").read_text()
    check("mutations audited", "catalog:add_model" in audit and "catalog:upsert_provider" in audit)

print("\nall catalog tests passed")
