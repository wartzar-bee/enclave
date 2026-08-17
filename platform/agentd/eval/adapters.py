#!/usr/bin/env python3
"""eval/adapters.py — task/dataset adapters for the eval runner.

An adapter = name + tasks(opts) → [{id, prompt, think, max_tokens, …}] + grade(task, text) →
(ok, detail) + summarize(rows) → dict. Ships: `capability` (6-task battery: reasoning/coding/json/
extraction/classification/instruction) and `gsm8k` (external gradeable math — rows from --data or the
no-install HF datasets-server REST). vision/tool-calling adapters come later; no plugin SPI until a
third adapter demands one.

Grading always strips reasoning traces first — grade the FINAL answer, never the thinking.
"""
import json, re, urllib.request


def strip_reasoning(o):
    """Remove reasoning traces (qwen <think>, gemma <|channel>thought, trace-then-</think>)."""
    o = re.sub(r"<think>.*?</think>", "", o, flags=re.S)
    o = re.sub(r"<\|channel>thought.*?<channel\|>", "", o, flags=re.S)
    o = re.sub(r"^.*</think>", "", o, flags=re.S)
    o = re.sub(r"<\|channel>thought.*$", "", o, flags=re.S)
    for t in ("<think>", "</think>", "<channel|>", "<|channel>thought", "<|think|>"):
        o = o.replace(t, "")
    return o.strip()


def truncated(o):
    """A reasoning block opened but never closed = the token budget cut it off."""
    return ("<think>" in o and "</think>" not in o) or ("<|channel>thought" in o and "<channel|>" not in o)


class Capability:
    """6-task capability battery. Reasoning runs thinking-on (where supported); structured tasks off."""
    name = "capability"
    BATTERY = [
        ("reasoning", "A farmer has 17 sheep. All but 9 run away. He buys 4 more, then half of his "
         "current flock die. How many sheep now? Reason briefly, end with 'ANSWER: <n>'.", True, 4096),
        ("coding", "Write a Python function `dedupe_keep_order(xs)` that removes duplicates from a list "
         "while preserving first-seen order. Return ONLY the code in a ```python block.", False, 512),
        ("json_format", "Extract to JSON with exactly keys name(str), age(int), city(str). Text: "
         "'Marcus, a 41-year-old architect, has lived in Lisbon for a decade.' Output ONLY the JSON object.", False, 256),
        ("extraction", "From this changelog line pull the semver and change type as JSON {version, type}: "
         "'v2.3.1 - fix: guard against null tokens in the loader'. ONLY JSON.", False, 256),
        ("classification", "Classify sentiment as exactly one word (positive/negative/neutral): "
         "'The install was painless but the docs are thin.' One word only.", False, 64),
        ("instruction", "List 3 risks of auto-installing third-party code plugins. Exactly 3 bullets, "
         "<=12 words each.", False, 512),
    ]

    def tasks(self, opts):
        return [{"id": n, "prompt": p, "think": t, "max_tokens": m} for n, p, t, m in self.BATTERY]

    def grade(self, task, text):
        o = strip_reasoning(text)
        t = task["id"]
        try:
            if t == "reasoning":
                m = re.search(r"ANSWER:\s*(\d+)", o)
                return (m is not None and "13" in o), (m.group(1) if m else "no ANSWER")
            if t == "coding":
                code = re.search(r"```(?:python)?\s*(.+?)```", o, re.S)
                body = code.group(1) if code else o
                ok = "def dedupe_keep_order" in body and \
                     ("seen" in body or "dict.fromkeys" in body or "not in" in body)
                return ok, "def+dedup" if ok else "missing"
            if t in ("json_format", "extraction"):
                js = re.search(r"\{.*\}", o, re.S)
                d = json.loads(js.group(0)) if js else None
                if t == "json_format":
                    ok = d and set(d) >= {"name", "age", "city"} and isinstance(d.get("age"), int)
                else:
                    ok = d and "2.3.1" in str(d.get("version", "")) and str(d.get("type", "")).lower() == "fix"
                return bool(ok), (json.dumps(d) if d else "no-json")
            if t == "classification":
                m = re.search(r"[A-Za-z]+", o)
                w = m.group(0).lower() if m else ""
                return w in ("positive", "negative", "neutral"), w[:20]
            if t == "instruction":
                bullets = [l for l in o.splitlines()
                           if l.strip().startswith(("-", "*", "•")) or re.match(r"^\s*\d+[.)]", l)]
                return len(bullets) == 3, f"{len(bullets)} bullets"
        except Exception as e:
            return False, f"err:{e}"
        return None, ""

    def summarize(self, rows):
        return {"score": sum(1 for r in rows if r["ok"]), "n": len(rows),
                "errors": sum(1 for r in rows if r["error"]),
                "avg_secs": round(sum(r["secs"] for r in rows) / max(1, len(rows)), 1)}


class GSM8K:
    """External math benchmark, exact-match numeric grade. Rows: opts['data'] (a local json of
    [{question, answer '… #### N'}]) or fetched from the HF datasets-server REST (no install)."""
    name = "gsm8k"
    HF = ("https://datasets-server.huggingface.co/rows?dataset=openai%2Fgsm8k"
          "&config=main&split=test&offset=0&length={n}")
    NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

    def _rows(self, opts):
        n = int(opts.get("n", 50))
        if opts.get("data"):
            return json.load(open(opts["data"]))[:n]
        with urllib.request.urlopen(self.HF.format(n=min(n, 100)), timeout=60) as r:
            return [x["row"] for x in json.loads(r.read())["rows"]][:n]

    def tasks(self, opts):
        out = []
        for i, r in enumerate(self._rows(opts)):
            gold = self._val(r["answer"].split("####")[-1].strip())
            out.append({"id": f"gsm8k-{i}", "gold": gold, "think": True, "max_tokens": 3072,
                        "prompt": "Solve this math problem step by step. End with 'ANSWER: <number>' "
                                  "(the final number only, no units).\n\nProblem: " + r["question"]})
        return out

    def _val(self, s):
        s = s.replace(",", "").rstrip(".")
        try:
            f = float(s)
            return int(f) if f == int(f) else round(f, 4)
        except Exception:
            return None

    def grade(self, task, text):
        ans = strip_reasoning(text)
        m = re.search(r"ANSWER:\s*\$?(-?[\d,]+(?:\.\d+)?)", ans, re.I)
        pred = self._val(m.group(1)) if m else \
            (self._val(self.NUM.findall(ans)[-1]) if self.NUM.findall(ans) else None)
        ok = pred is not None and task["gold"] is not None and pred == task["gold"]
        return ok, f"pred={pred} gold={task['gold']}" + (" TRUNC" if truncated(text) else "")

    def summarize(self, rows):
        n = max(1, len(rows))
        return {"correct": sum(1 for r in rows if r["ok"]), "n": len(rows),
                "acc": round(100 * sum(1 for r in rows if r["ok"]) / n, 1),
                "truncated": sum(1 for r in rows if "TRUNC" in (r["detail"] or "")),
                "errors": sum(1 for r in rows if r["error"]),
                "avg_secs": round(sum(r["secs"] for r in rows) / n, 1)}


class BigData:
    """Harness-comparison adapter: exact counting over a LARGE structured file — the task class
    where harness design, not model quality, decides the outcome (2026-08-17 pilot: same worker
    model answered exact-correct via pyexec at ~2k tokens and WRONG via rlm map-reduce at 638k).

    The fixture is SYNTHESIZED deterministically (seeded) so no real agent log ever enters the
    repo or the eval trail; gold counts are known by construction. --data <events.jsonl> swaps in
    a local real log instead (gold computed by counting it directly; file stays local).
    Opts: harness pyexec|rlm|both (default pyexec — rlm on a big fixture is slow and token-hungry,
    that is the point of measuring it); n = reps per harness (default 3, capped 10); seed; size.
    """
    name = "bigdata"
    TOOLS = [("Bash", 12, 0.04), ("Read", 4, 0.0), ("Write", 3, 0.01), ("Edit", 2, 0.08),
             ("WebSearch", 2, 0.0), ("Agent", 1, 0.0)]   # (tool, weight, failure probability)
    QUERY = ('Report exactly: (a) total number of tool events (event=="tool"), (b) calls per '
             'tool, (c) failures (ok is False) per tool, (d) total failures. Count, do not estimate.')

    def _fixture(self, opts):
        """→ (path, gold). Deterministic per (seed, size); cached on disk under state/evals."""
        import os, pathlib, random
        if opts.get("data"):
            return self._gold_from(opts["data"])
        seed, size = int(opts.get("seed", 1177)), int(opts.get("size", 1500))
        d = pathlib.Path(os.environ.get("AGENT_DIR", ".")) / "state" / "evals"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"bigdata-fixture-{seed}-{size}.jsonl"
        rng = random.Random(seed)
        pool = [t for t, w, _ in self.TOOLS for _ in range(w)]
        prob = {t: p for t, _, p in self.TOOLS}
        calls, fails, lines = {}, {}, []
        words = ("configure retry queue vault policy egress tick snapshot broker index "
                 "candidate gate review deploy probe").split()
        for i in range(size):
            t = rng.choice(pool)
            ok = rng.random() >= prob[t]
            calls[t] = calls.get(t, 0) + 1
            if not ok:
                fails[t] = fails.get(t, 0) + 1
            filler = " ".join(rng.choice(words) for _ in range(rng.randint(30, 60)))
            lines.append(json.dumps({"ts": 1780000000 + i, "event": "tool", "tool": t,
                                     "summary": f"step {i}: {filler[:80]}", "ok": ok,
                                     "result": filler if ok else f"error: {filler[:120]} (exit 1)"}))
        content = "\n".join(lines) + "\n"
        if not (path.exists() and path.read_text() == content):
            path.write_text(content)
        gold = {"total": size, "calls": calls, "fails": fails, "total_fails": sum(fails.values())}
        return str(path), gold

    def _gold_from(self, data_path):
        calls, fails, total = {}, {}, 0
        for ln in open(data_path):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("event") != "tool":
                continue
            total += 1
            t = d.get("tool", "?")
            calls[t] = calls.get(t, 0) + 1
            if d.get("ok") is False:
                fails[t] = fails.get(t, 0) + 1
        return data_path, {"total": total, "calls": calls, "fails": fails,
                           "total_fails": sum(fails.values())}

    def tasks(self, opts):
        path, gold = self._fixture(opts)
        harnesses = {"pyexec": ["pyexec"], "rlm": ["rlm"],
                     "both": ["pyexec", "rlm"]}[opts.get("harness") or "pyexec"]
        n = opts.get("n")
        reps = 3 if n in (None, "", 50) else min(max(1, int(n)), 10)   # 50 = the cli default, not a wish for 50 reps
        return [{"id": f"{h}-count-r{i+1}", "harness": h, "query": self.QUERY,
                 "file": path, "gold": gold, "max_tokens": 1024}
                for h in harnesses for i in range(reps)]

    def grade(self, task, text):
        g = task["gold"]
        o = strip_reasoning(text)
        missing = [str(v) for v in ([g["total"], g["total_fails"]] + list(g["fails"].values()))
                   if str(v) not in o]
        missing += [t for t in g["fails"] if t not in o]
        return (not missing), ("exact" if not missing else f"missing:{','.join(missing[:6])}")

    def summarize(self, rows):
        out = {}
        for h in sorted({r["task"].split("-")[0] for r in rows}):
            hr = [r for r in rows if r["task"].startswith(h)]
            n = max(1, len(hr))
            out[h] = {"correct": sum(1 for r in hr if r["ok"]), "n": len(hr),
                      "avg_secs": round(sum(r["secs"] for r in hr) / n, 1),
                      "avg_tokens": round(sum(r["tokens"] or 0 for r in hr) / n),
                      "avg_calls": round(sum(r.get("calls") or 0 for r in hr) / n, 1)}
        return {"harnesses": out, "n": len(rows),
                "errors": sum(1 for r in rows if r["error"])}


ADAPTERS = {"capability": Capability, "gsm8k": GSM8K, "bigdata": BigData}
