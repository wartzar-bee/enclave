#!/usr/bin/env python3
"""Tests for the eval primitive — adapters (grading), runner (endpoint resolution, params, retry-on-
strict-endpoint), catalog model_params/record_eval. Offline: the endpoint is a local stub server.

Run: python3 test_eval.py"""
import json, os, pathlib, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).parent / "eval"))
import catalog
import runner
from adapters import ADAPTERS, Capability, GSM8K, strip_reasoning, truncated


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    assert cond, name


with tempfile.TemporaryDirectory() as d:
    os.environ["ENCLAVE_CONSOLE_CATALOG"] = str(pathlib.Path(d) / "cat.json")
    os.environ["ENCLAVE_FLEET_AUDIT"] = str(pathlib.Path(d) / "audit.log")

    # ── reasoning-trace stripping ──
    check("strip <think>", strip_reasoning("<think>blah</think>ANSWER: 13") == "ANSWER: 13")
    check("strip trace-then-close", strip_reasoning("step1 step2</think>final") == "final")
    check("truncated open think", truncated("<think>never closed"))
    check("closed think not truncated", not truncated("<think>x</think>done"))

    # ── capability grading ──
    cap = Capability()
    tasks = {t["id"]: t for t in cap.tasks({})}
    check("battery is 6 tasks", len(tasks) == 6)
    check("reasoning thinks, structured doesn't", tasks["reasoning"]["think"] and not tasks["coding"]["think"])
    check("reasoning grades 13", cap.grade(tasks["reasoning"], "so ANSWER: 13")[0])
    check("reasoning rejects 9", not cap.grade(tasks["reasoning"], "ANSWER: 9")[0])
    check("coding grades fenced def", cap.grade(tasks["coding"],
          "```python\ndef dedupe_keep_order(xs):\n  seen=set()\n```")[0])
    check("json_format needs int age", not cap.grade(tasks["json_format"],
          '{"name":"M","age":"41","city":"Lisbon"}')[0])
    check("json_format ok", cap.grade(tasks["json_format"],
          '{"name":"Marcus","age":41,"city":"Lisbon"}')[0])
    check("extraction ok", cap.grade(tasks["extraction"], '{"version":"v2.3.1","type":"fix"}')[0])
    check("classification word", cap.grade(tasks["classification"], "Neutral.")[0])
    check("instruction 3 bullets", cap.grade(tasks["instruction"], "- a\n- b\n- c")[0])
    check("instruction wrong count", not cap.grade(tasks["instruction"], "- a\n- b")[0])

    # ── gsm8k grading (local data, no network) ──
    rows = [{"question": "2+2?", "answer": "add\n#### 4"},
            {"question": "10-3?", "answer": "sub\n#### 7"}]
    data = pathlib.Path(d) / "rows.json"
    data.write_text(json.dumps(rows))
    g = GSM8K()
    ts = g.tasks({"data": str(data), "n": 2})
    check("gsm8k parses gold", ts[0]["gold"] == 4 and ts[1]["gold"] == 7)
    check("gsm8k ANSWER match", g.grade(ts[0], "<think>hmm</think>ANSWER: 4")[0])
    check("gsm8k last-number fallback", g.grade(ts[1], "10-3 = 7")[0])
    check("gsm8k wrong", not g.grade(ts[0], "ANSWER: 5")[0])
    check("gsm8k comma number", g.grade({"gold": 1200}, "ANSWER: 1,200")[0])

    # ── catalog model_params: seeded + pair resolution + CRUD ──
    p = catalog.model_params("mlx-community/Qwen3-8B-4bit")
    check("seeded params present", p and p["can_think"])
    check("pair resolves think", runner.pick(p["temp"], True) == 0.6)
    check("pair resolves no-think", runner.pick(p["temp"], False) == 0.7)
    check("unknown model → None", catalog.model_params("nope/nope") is None)
    r = catalog.set_model_params("test/model-1", {"temp": 0.5, "top_p": 0.9, "can_think": False})
    check("set_model_params ok", r.get("ok") and catalog.model_params("test/model-1")["temp"] == 0.5)
    pf = runner.params_for("test/model-1", False)
    check("params_for catalog source", pf["source"] == "catalog" and pf["temperature"] == 0.5)
    check("params_for default tagged", runner.params_for("nope/nope", False)["source"] == "default")

    # ── catalog eval evidence trail ──
    for i in range(12):
        catalog.record_eval("test/model-1", {"adapter": "gsm8k", "acc": i})
    trail = catalog.load()["eval_results"]["test/model-1"]
    check("record_eval caps at 10", len(trail) == 10 and trail[-1]["acc"] == 11)
    audit = (pathlib.Path(d) / "audit.log").read_text()
    check("eval mutations audited", "catalog:set_model_params" in audit and "catalog:record_eval" in audit)

    # ── endpoint resolution: --base wins; policy pool; catalog provider; unknown errors ──
    ep = runner.resolve_endpoint(base="http://x:1/v1/")
    check("--base wins, trailing / stripped", ep["base"] == "http://x:1/v1")
    pol = pathlib.Path(d) / "policy.json"
    pol.write_text(json.dumps({"pools": {"mlx": {"base_url_default": "http://h:8081/v1",
                                                 "api_key_default": "mlx"}}}))
    ep = runner.resolve_endpoint(pool="mlx", policy_path=str(pol))
    check("policy pool resolves", ep["base"] == "http://h:8081/v1" and ep["key"] == "mlx")
    ep = runner.resolve_endpoint(pool="nvidia", policy_path=str(pol))
    check("catalog provider fallback", "nvidia" in ep["label"] and ep["base"].startswith("https://integrate"))
    try:
        runner.resolve_endpoint(pool="ghost", policy_path=str(pol))
        check("unknown pool raises", False)
    except ValueError:
        check("unknown pool raises", True)

    # ── runner end-to-end vs a stub endpoint (strict: rejects extras once, like a picky API) ──
    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "top_k" in body or "chat_template_kwargs" in body:   # strict endpoint: reject extras
                self.send_response(400); self.end_headers(); self.wfile.write(b"extras")
                return
            ans = "ANSWER: 4" if "2+2" in body["messages"][0]["content"] else "ANSWER: 0"
            out = {"choices": [{"message": {"content": ans}}], "usage": {"completion_tokens": 3}}
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(out).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = {"base": f"http://127.0.0.1:{srv.server_port}", "key": "", "label": "stub"}
    out = pathlib.Path(d) / "res.jsonl"
    s = runner.run(GSM8K(), ["mlx-community/Qwen3-8B-4bit"], ep, str(out),
                   {"data": str(data), "n": 2})[0]
    srv.shutdown()
    check("stub run: 1 correct of 2", s["correct"] == 1 and s["n"] == 2)
    check("summary carries model+source", s["model"].endswith("Qwen3-8B-4bit") and s["params_source"] == "catalog")
    rows_out = [json.loads(l) for l in out.read_text().splitlines()]
    check("jsonl row per task", len(rows_out) == 2)
    check("strict endpoint retried w/o extras", all(r["extras_stripped"] for r in rows_out))
    check("no transport errors", all(not r["error"] for r in rows_out))

print("\nall eval tests passed")
