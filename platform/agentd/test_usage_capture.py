#!/usr/bin/env python3
"""Hermetic tests for usage_capture.py — the cost metric every $ control gates on.

Covers the 2026-07-04 calibration fix: cost_est must be the raw token estimate multiplied by the
learned per-model (actual/estimate) ratio, the ratio must fold toward the authoritative
total_cost_usd at every result event, and the end-to-end stream pipeline must emit a calibrated
.ctx-budget.json + a usage.jsonl record + a closed budget-calibration.jsonl line.
(The pre-fix metric ran ~7× the real bill on stoneforge — real $0.43 read as $3.04 — and this
computation had zero test coverage. Run: python3 test_usage_capture.py)
"""
import json, os, pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import usage_capture as uc


def test_raw_est_uses_list_rates():
    cum = {"input": 1_000_000, "cache_read": 1_000_000, "cache_write": 1_000_000, "output": 1_000_000}
    # 15 + 1.5 + 18.75 + 75 at the default rates
    assert abs(uc._raw_est(cum) - 110.25) < 1e-6, uc._raw_est(cum)


def test_cal_ratio_defaults_to_1():
    assert uc._cal_ratio({}, "claude-opus-4-8") == 1.0
    assert uc._cal_ratio({"m": {"ratio": 0}}, "m") == 1.0          # zero/invalid → 1.0
    assert uc._cal_ratio({"m": {"ratio": "x"}}, "m") == 1.0
    assert uc._cal_ratio({"m": {"ratio": 0.4}}, "m") == 0.4
    assert uc._cal_ratio({"m": {"ratio": 0.4}}, None) == 1.0       # no model → 1.0


def test_update_cal_first_observation_sets_ratio():
    with tempfile.TemporaryDirectory() as d:
        sd = pathlib.Path(d)
        cal = {}
        uc._update_cal(sd, cal, "m", raw_est=10.0, actual=4.0)
        saved = json.loads((sd / ".cost-calibration.json").read_text())
        assert abs(saved["m"]["ratio"] - 0.4) < 1e-6, saved
        assert saved["m"]["n"] == 1


def test_update_cal_ema_folds_toward_new_observation():
    with tempfile.TemporaryDirectory() as d:
        sd = pathlib.Path(d)
        cal = {"m": {"ratio": 0.4, "n": 5}}
        uc._update_cal(sd, cal, "m", raw_est=10.0, actual=8.0)     # obs = 0.8
        saved = json.loads((sd / ".cost-calibration.json").read_text())
        assert abs(saved["m"]["ratio"] - (0.7 * 0.4 + 0.3 * 0.8)) < 1e-6, saved
        assert saved["m"]["n"] == 6


def test_update_cal_ignores_garbage():
    with tempfile.TemporaryDirectory() as d:
        sd = pathlib.Path(d)
        for raw, actual in ((0, 1.0), (10.0, None), (10.0, 0), (-1, 1)):
            uc._update_cal(sd, {}, "m", raw_est=raw, actual=actual)
        assert not (sd / ".cost-calibration.json").exists()
        uc._update_cal(sd, {}, None, raw_est=10.0, actual=1.0)      # no model
        assert not (sd / ".cost-calibration.json").exists()


def _assistant(model, inp, cr, cw, out):
    return json.dumps({"type": "assistant", "message": {"model": model, "usage": {
        "input_tokens": inp, "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw, "output_tokens": out}, "content": []}})


def _result(cost, turns=2):
    return json.dumps({"type": "result", "subtype": "success", "total_cost_usd": cost,
                       "duration_ms": 1000, "num_turns": turns, "is_error": False,
                       "usage": {"input_tokens": 2, "output_tokens": 200,
                                 "cache_read_input_tokens": 200000,
                                 "cache_creation_input_tokens": 100}})


def test_stream_pipeline_applies_seeded_ratio_and_closes_loops():
    """End-to-end: seeded ratio 0.5 → .ctx-budget cost_est == cost_raw*0.5; result event updates
    the calibration EMA and appends a REAL actual to budget-calibration.jsonl."""
    with tempfile.TemporaryDirectory() as d:
        sd = pathlib.Path(d) / "state"
        sd.mkdir()
        (sd / ".cost-calibration.json").write_text(json.dumps({"test-model": {"ratio": 0.5, "n": 3}}))
        (sd / "budget.json").write_text(json.dumps({"package": "pkg-x", "soft_usd": 2, "hard_usd": 4}))
        stream = "\n".join([
            _assistant("test-model", 100, 100000, 1000, 500),
            _assistant("test-model", 100, 100000, 1000, 500),
            _result(0.42),
        ]) + "\n"
        out = sd / "usage.jsonl"
        p = subprocess.run([sys.executable, str(HERE / "usage_capture.py"),
                            "--agent", "t", "--reason", "heartbeat", "--out", str(out)],
                           input=stream, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr

        bud = json.loads((sd / ".ctx-budget.json").read_text())
        assert bud["turn"] == 2
        assert abs(bud["cost_est"] - bud["cost_raw"] * 0.5) < 1e-3, bud   # seeded ratio applied

        rec = json.loads(out.read_text().splitlines()[-1])
        assert rec["cost_usd"] == 0.42 and rec["subtype"] == "success", rec

        cal = json.loads((sd / ".cost-calibration.json").read_text())
        raw = uc._raw_est({"input": 200, "cache_read": 200000, "cache_write": 2000, "output": 1000})
        expect = 0.7 * 0.5 + 0.3 * (0.42 / raw)
        assert abs(cal["test-model"]["ratio"] - expect) < 1e-3, (cal, expect)
        assert cal["test-model"]["n"] == 4

        bc = [json.loads(l) for l in (sd / "budget-calibration.jsonl").read_text().splitlines()]
        assert bc[-1]["package"] == "pkg-x" and bc[-1]["actual_usd"] == 0.42, bc  # loop closed with truth


def test_stream_pipeline_unknown_model_ratio_1():
    """No calibration entry → cost_est == cost_raw (conservative over-estimate, self-corrects)."""
    with tempfile.TemporaryDirectory() as d:
        sd = pathlib.Path(d) / "state"
        sd.mkdir()
        stream = _assistant("fresh-model", 100, 100000, 1000, 500) + "\n" + _result(0.1) + "\n"
        p = subprocess.run([sys.executable, str(HERE / "usage_capture.py"),
                            "--agent", "t", "--reason", "heartbeat", "--out", str(sd / "usage.jsonl")],
                           input=stream, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        bud = json.loads((sd / ".ctx-budget.json").read_text())
        assert abs(bud["cost_est"] - bud["cost_raw"]) < 1e-6, bud
        cal = json.loads((sd / ".cost-calibration.json").read_text())
        assert cal["fresh-model"]["n"] == 1                      # learned from the first tick


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n}/{n} passed")
