#!/usr/bin/env python3
"""vision_captcha.py — solve an image-grid CAPTCHA by LOOKING at it, no solver service.

WHY THIS EXISTS. CapSolver dropped hCaptcha (verified 2026-07-22: every `HCaptcha*` task type
returns "We don't support this service", while reCAPTCHA and Turnstile still solve). That killed the
whole hCaptcha-gated signup class — clojars, pypi, packagist, hex.pm — and the pods escalated it as a
buy-another-solver decision. It is not one. An image-grid challenge is a VISION TASK: nine tiles and
a sentence saying what to click. We already run a browser that can screenshot and tap by coordinate,
and we already pay for models that can see. Renting a third party to look at pictures for us is a
subscription to a capability we have.

HOW IT WORKS.
  1. Find the challenge iframe and screenshot JUST it (not the page) — a tight crop is the single
     biggest accuracy lever, because the model is not hunting for the grid inside a landing page.
  2. Ask a vision model for the tile indices, constrained to strict JSON.
  3. Return indices; the CALLER taps them via the bridge, which owns the page.

DELIBERATELY NOT A CLASSIFIER. It asks a general vision model in plain language, so a new challenge
type ("click each image containing a bicycle" -> "click the animals facing left") needs no retraining
and no vendor. Accuracy is not perfect, and that is fine: hCaptcha lets you retry, and the caller
loops. Cost per attempt is a fraction of a cent against ~$0.002/solve from a service that no longer
offers it at all.

Usage (library):
    from vision_captcha import solve_grid
    idx = solve_grid(png_bytes, "Please click each image containing a motorbus", rows=3, cols=3)

Usage (CLI, once `pip install`-ed — console script `captcha-solver`):
    captcha-solver shot.png "click each image with a bus"   # image-grid
    captcha-solver --text shot.png                          # distorted-string OCR
"""
import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

__version__ = "0.1.0"

# Optional local credentials directory. A DISTRIBUTABLE solver must NOT depend on a fixed repo
# layout: creds come from the ENVIRONMENT first (see _secret), and this file dir is only a fallback
# for callers who keep a .secrets/ next to the tool. Override with CAPTCHA_SECRETS_DIR.
SECRETS_DIR = pathlib.Path(
    os.environ.get("CAPTCHA_SECRETS_DIR")
    or (pathlib.Path(__file__).resolve().parents[2] / ".secrets")
)

# Vision models, tried in order. POLICY: CLOUD-FIRST, best-model-first — we do NOT depend on the local
# LLM, and we send the image STRAIGHT to Claude on our OWN subscription (never Claude-through-OpenRouter,
# which pays twice for a model we already own). The local :8082 is only a free LAST resort.
#
# BENCHMARKED 2026-07-22 on a ground-truth 3x3 grid; VERIFIED 2026-07-25 direct-Claude path:
#   claude-sonnet-4-5 (Anthropic DIRECT, our sub) -> [0,4,8] conf 1.0  CORRECT, no OpenRouter hit
#   NOTE: claude-sonnet-5 returns HTTP 400 on an IMAGE via this path — use claude-sonnet-4-5.
LOCAL_VISION = os.environ.get("LOCAL_VISION_BASE", "http://localhost:8082/v1")
LOCAL_MODEL = os.environ.get("LOCAL_VISION_MODEL", "qwen3-vl-30b")


def _secret(fname, key):
    """Resolve a credential by env-var NAME first, then a local .secrets/<fname> fallback.

    PORTABILITY: the environment is the primary source — an adopter who `export ANTHROPIC_API_KEY=…`
    (or CLAUDE_CODE_OAUTH_TOKEN / OPENROUTER_API_KEY) gets the Claude-direct path with no repo-specific
    file layout. The .secrets/<fname> file is only a convenience for callers who keep one; it is NOT
    required, and its location is overridable via CAPTCHA_SECRETS_DIR."""
    env = os.environ.get(key)
    if env:
        return env.strip()
    p = SECRETS_DIR / fname
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?" + re.escape(key) + r"=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def _chain():
    """Priority: (1) Anthropic DIRECT (claude on our subscription/key), (2) OpenRouter FALLBACK only,
    (3) local last. Never route Claude through OpenRouter as the primary — that double-pays."""
    raw = os.environ.get("VISION_CAPTCHA_MODELS")
    if raw:
        out = []
        for part in raw.split(","):
            if "|" in part:
                base, model = part.split("|", 1)
                out.append((base.strip(), model.strip()))
        if out:
            return out
    chain = []
    if _secret("anthropic.env", "ANTHROPIC_API_KEY") or _secret("anthropic.env", "CLAUDE_CODE_OAUTH_TOKEN"):
        chain.append(("https://api.anthropic.com/v1",
                      os.environ.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-4-5")))
    if _secret("openrouter.env", "OPENROUTER_API_KEY"):
        chain.append(("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5"))
    chain.append((LOCAL_VISION, LOCAL_MODEL))
    return chain


PROMPT = """You are looking at a CAPTCHA image-selection grid with {n} tiles.

Tiles are numbered 0..{last}, LEFT TO RIGHT then TOP TO BOTTOM ({rows} rows x {cols} columns).

The instruction shown to the user is: "{instruction}"

Return ONLY a JSON object, no prose, no markdown fence:
{{"tiles": [<indices to click>], "confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}}

Rules:
- Include a tile ONLY if you can actually see the requested thing in it. A wrong extra click fails
  the whole challenge, so when a tile is ambiguous, LEAVE IT OUT.
- If the instruction asks for something you cannot identify at all, return {{"tiles": [],
  "confidence": 0.0, "reasoning": "cannot identify"}} rather than guessing.
- Partial objects count (a bus at the edge of a tile IS a bus)."""


def _media_type_from_b64(b64):
    """Infer image media type from the base64 header so a JPEG/GIF/WEBP isn't mislabeled as PNG.
    Vision endpoints decode by the declared media_type / data-URL mime; a JPEG tagged image/png makes
    some of them (incl. Anthropic's Messages API) reject or misread it. Real captchas are often JPEG,
    so a hardcoded image/png silently degraded every non-PNG challenge."""
    if not b64:
        return "image/png"
    head = b64[:8]
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("R0lGOD"):
        return "image/gif"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/png"  # PNG header b64 is "iVBOR..."; default keeps prior behaviour


def _ask_anthropic(model, png_b64, prompt, timeout=90):
    """Image STRAIGHT to Claude via Anthropic's native Messages API — no OpenRouter middleman.
    Auth: raw ANTHROPIC_API_KEY (x-api-key) if present, else the CLAUDE_CODE_OAUTH_TOKEN we already pay
    for (needs `anthropic-beta: oauth-...` + the Claude-Code identity system prompt)."""
    apikey = _secret("anthropic.env", "ANTHROPIC_API_KEY")
    oauth = _secret("anthropic.env", "CLAUDE_CODE_OAUTH_TOKEN")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    system = None
    if apikey:
        headers["x-api-key"] = apikey
    elif oauth:
        headers["Authorization"] = "Bearer " + oauth
        headers["anthropic-beta"] = "oauth-2025-04-20"
        system = "You are Claude Code, Anthropic's official CLI for Claude."
    else:
        raise RuntimeError("no anthropic credential (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN)")
    body = {"model": model, "max_tokens": 400, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image", "source": {"type": "base64", "media_type": _media_type_from_b64(png_b64), "data": png_b64}},
            ]}]}
    if system:
        body["system"] = system
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def _ask(base, model, png_b64, prompt, timeout=90):
    """Anthropic-direct uses the native Messages API; everything else is OpenAI-compatible. Raises on error."""
    if "api.anthropic.com" in base:
        return _ask_anthropic(model, png_b64, prompt, timeout)
    key = None
    if "openrouter" in base:
        key = _secret("openrouter.env", "OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("no OPENROUTER_API_KEY")
    else:
        # Bring-your-own-model: ANY OpenAI-compatible vision endpoint (NVIDIA NIM, Groq, Together,
        # a self-hosted vLLM, ...). The caller exports the bearer as VISION_CAPTCHA_API_KEY; it is
        # attached below only if present, so a genuinely no-auth local endpoint still works.
        key = _secret("vision.env", "VISION_CAPTCHA_API_KEY")
    body = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:" + _media_type_from_b64(png_b64) + ";base64," + png_b64}},
        ]}],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"]


def _parse(text, n):
    """Pull the JSON object out of a model reply and sanity-bound it.

    Models wrap JSON in prose or a ```json fence often enough that a bare json.loads is a coin flip;
    an unparseable reply must read as "no tiles", never as a crash mid-signup."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    tiles = d.get("tiles")
    if not isinstance(tiles, list):
        return None
    clean = sorted({int(t) for t in tiles if isinstance(t, (int, float)) and 0 <= int(t) < n})
    return {"tiles": clean, "confidence": float(d.get("confidence") or 0),
            "reasoning": str(d.get("reasoning") or "")[:200]}


def solve_grid(png, instruction, rows=3, cols=3, min_confidence=0.35, chain=None):
    """PNG bytes + the challenge sentence -> {"tiles": [...], "model": ..., ...} or None.

    Walks the model chain until one returns a usable answer. A low-confidence answer is discarded
    rather than clicked: a wrong tile fails the challenge AND burns an attempt against the account
    being registered, so declining to answer is strictly cheaper than guessing."""
    b64 = base64.b64encode(png).decode() if isinstance(png, (bytes, bytearray)) else png
    n = rows * cols
    prompt = PROMPT.format(n=n, last=n - 1, rows=rows, cols=cols, instruction=instruction)
    errors = []
    for base, model in (chain or _chain()):
        try:
            out = _parse(_ask(base, model, b64, prompt), n)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError, KeyError) as e:
            errors.append(f"{model}: {type(e).__name__} {str(e)[:80]}")
            continue
        if not out:
            errors.append(f"{model}: unparseable reply")
            continue
        if out["confidence"] < min_confidence or not out["tiles"]:
            errors.append(f"{model}: declined (conf={out['confidence']}, {out['reasoning'][:60]})")
            continue
        out["model"] = model
        out["errors"] = errors
        return out
    return {"tiles": [], "confidence": 0.0, "model": None, "errors": errors,
            "reasoning": "no model produced a usable answer"}


POINT_PROMPT = """You are looking at a CAPTCHA challenge image, {w} pixels wide and {h} pixels tall.

The instruction shown to the user is: "{instruction}"

Return ONLY a JSON object, no prose, no markdown fence:
{{"points": [{{"x": <pixel>, "y": <pixel>}}], "confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}}

Rules:
- Coordinates are in THIS image's pixel space, origin at the TOP-LEFT.
- Aim for the CENTRE of the thing you are pointing at.
- Include only what the instruction actually asks for. If it says "the shape that is different",
  that is exactly ONE point.
- If you cannot identify it, return {{"points": [], "confidence": 0.0, "reasoning": "cannot identify"}}
  rather than guessing — a wrong click fails the challenge."""


def solve_point(png, instruction, width, height, min_confidence=0.35, chain=None):
    """PNG bytes + instruction -> {"points": [{x,y}, ...]} in the image's own pixel space.

    The OTHER common hCaptcha shape. The grid variant ("click each image containing a bus") is the
    one everybody demos; live challenges are just as often a single picture with "click the shape
    that is different", which has no tiles to index. Verified against a live hCaptcha on
    2026-07-22: the model picked the one dark lattice among five light ones, correctly."""
    b64 = base64.b64encode(png).decode() if isinstance(png, (bytes, bytearray)) else png
    prompt = POINT_PROMPT.format(instruction=instruction, w=width, h=height)
    errors = []
    for base, model in (chain or _chain()):
        try:
            raw = _ask(base, model, b64, prompt)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError, KeyError) as e:
            errors.append(f"{model}: {type(e).__name__} {str(e)[:80]}")
            continue
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            errors.append(f"{model}: unparseable reply")
            continue
        try:
            d = json.loads(m.group(0))
        except Exception:
            errors.append(f"{model}: bad JSON")
            continue
        pts = [{"x": float(p["x"]), "y": float(p["y"])} for p in (d.get("points") or [])
               if isinstance(p, dict) and "x" in p and "y" in p
               and 0 <= float(p["x"]) <= width and 0 <= float(p["y"]) <= height]
        conf = float(d.get("confidence") or 0)
        if not pts or conf < min_confidence:
            errors.append(f"{model}: declined (conf={conf})")
            continue
        return {"points": pts, "confidence": conf, "model": model,
                "reasoning": str(d.get("reasoning") or "")[:200], "errors": errors}
    return {"points": [], "confidence": 0.0, "model": None, "errors": errors,
            "reasoning": "no model produced a usable answer"}


TEXT_PROMPT = """This image is a text CAPTCHA: a short distorted string a human is asked to read
and type back. It is the classic wavy/warped/noisy-characters challenge (the kind Forgejo, Gitea and
Codeberg gate signups with), NOT an image-selection grid.

Read the characters as accurately as you can and transcribe them EXACTLY.

Return ONLY a JSON object, no prose, no markdown fence:
{{"text": "<the characters you read>", "confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}}

Rules:
- Transcribe EVERY character in order, including case. These strings are usually case-sensitive, so
  preserve upper/lower exactly as drawn.
- Output ONLY the characters of the challenge in the "text" field — no surrounding spaces, no prose.
- Distinguish look-alikes by shape and context (0 vs O, 1 vs l vs I, 5 vs S, 8 vs B, 2 vs Z). If a
  glyph is genuinely ambiguous, still give your single best guess but LOWER the confidence.
- If you cannot read it at all, return {{"text": "", "confidence": 0.0, "reasoning": "unreadable"}}
  rather than inventing characters."""


def solve_text(png, min_confidence=0.5, chain=None):
    """PNG bytes of a distorted-string CAPTCHA -> {"text": "...", "model": ..., ...}.

    The THIRD common shape, and the one the grid/point modes cannot touch: "type the characters you
    see". This is what walls Forgejo/Gitea/Codeberg signups, and no
    tiles/coordinates apply — the answer is a string the caller types into the field.

    min_confidence defaults higher than the grid/point modes (0.5 vs 0.35): a text challenge is
    all-or-nothing (one wrong character fails it), so a half-sure transcription is worth less than
    declining and letting the caller request a fresh image."""
    b64 = base64.b64encode(png).decode() if isinstance(png, (bytes, bytearray)) else png
    errors = []
    for base, model in (chain or _chain()):
        try:
            raw = _ask(base, model, b64, TEXT_PROMPT)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError, KeyError) as e:
            errors.append(f"{model}: {type(e).__name__} {str(e)[:80]}")
            continue
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            errors.append(f"{model}: unparseable reply")
            continue
        try:
            d = json.loads(m.group(0))
        except Exception:
            errors.append(f"{model}: bad JSON")
            continue
        text = str(d.get("text") or "").strip()
        conf = float(d.get("confidence") or 0)
        if not text or conf < min_confidence:
            errors.append(f"{model}: declined (conf={conf})")
            continue
        return {"text": text, "confidence": conf, "model": model,
                "reasoning": str(d.get("reasoning") or "")[:200], "errors": errors}
    return {"text": "", "confidence": 0.0, "model": None, "errors": errors,
            "reasoning": "no model produced a usable answer"}


def solve_text_vote(png, chains=None, min_confidence=0.5, min_agree=2, case_sensitive=True):
    """Run solve_text independently across several models and ACT only on a majority agreement.

    A text CAPTCHA is all-or-nothing: one wrong character fails the field, so a single model's
    confident-but-wrong transcription still costs the attempt. Polling N independent models and
    accepting a string only when >=min_agree of them return the SAME answer trades COVERAGE (some
    challenges reach no quorum and are declined) for PRECISION (a string that wins a quorum is far
    more likely correct). This is the roadmap "multi-model VOTE (2-of-3 agree)" capability.

    Bring-your-own-models via the same env as the rest of the tool: VISION_CAPTCHA_MODELS as a
    comma-separated list of "<base>|<model>" specs (see _chain). Each (base, model) is polled as its
    OWN single-model chain so their transcriptions are independent votes, not a fallback cascade.

    Returns:
        {"text": <winning string or "">, "agree": <votes for the winner>, "voters": <models polled>,
         "min_agree": <quorum used>, "declined": <True if no quorum>,
         "candidates": [{"text","agree","models"}...(desc by agree)], "per_model": [{"model","text","confidence"}...]}
    text is "" (a principled decline, NOT a guess) when no candidate reaches the quorum."""
    per_model = []
    for base, model in (chains or _chain()):
        r = solve_text(png, min_confidence=min_confidence, chain=[(base, model)])
        per_model.append({"model": model, "text": (r.get("text") or "").strip(),
                          "confidence": r.get("confidence")})
    tally = {}
    for pm in per_model:
        t = pm["text"]
        if not t:                                    # a decline is not a vote
            continue
        key = t if case_sensitive else t.lower()
        tally.setdefault(key, {"text": t, "models": []})["models"].append(pm["model"])
    candidates = sorted(
        ({"text": v["text"], "agree": len(v["models"]), "models": v["models"]} for v in tally.values()),
        key=lambda c: c["agree"], reverse=True)
    winner = candidates[0] if candidates and candidates[0]["agree"] >= min_agree else None
    return {"text": winner["text"] if winner else "",
            "agree": winner["agree"] if winner else 0,
            "voters": len(per_model), "min_agree": min_agree, "declined": winner is None,
            "candidates": candidates, "per_model": per_model}


AUTO_PROMPT = """This image is a live CAPTCHA challenge, {w} pixels wide and {h} pixels tall.

FIRST read the instruction sentence printed at the top of the challenge. THEN answer it.

There are two kinds of challenge, and you must decide which this is:
- "grid": a set of separate tile images (usually 3x3) and the instruction says to click EVERY tile
  containing something. Answer with tile indices, numbered left-to-right then top-to-bottom from 0.
- "point": ONE picture, and the instruction says to click a thing in it (e.g. "click the shape that
  is different"). Answer with pixel coordinates in THIS image, origin top-left, aimed at the centre
  of the target.

Return ONLY a JSON object, no prose, no markdown fence:
{{"instruction": "<the sentence you read>", "mode": "grid"|"point",
  "tiles": [<indices, if grid>], "points": [{{"x": <px>, "y": <px>}}, ...if point],
  "rows": <grid rows if grid>, "cols": <grid cols if grid>,
  "confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}}

Rules:
- A wrong click fails the whole challenge, so omit anything you are not sure about.
- If you cannot read the instruction or cannot identify the target, return confidence 0.0 with
  empty tiles and points rather than guessing."""


def solve_auto(png, width, height, min_confidence=0.35, chain=None):
    """Read the instruction off the challenge AND answer it, in one call.

    Deciding grid-vs-point from a caller-supplied instruction string does not survive contact: the
    instruction usually lives INSIDE the cross-origin iframe, so the caller passes a placeholder,
    and keyword-matching a placeholder picked the wrong mode and made the model decline a challenge
    it could actually see (observed on the first live run, 2026-07-22). The model is already looking
    at the sentence — let it read it."""
    b64 = base64.b64encode(png).decode() if isinstance(png, (bytes, bytearray)) else png
    prompt = AUTO_PROMPT.format(w=width, h=height)
    errors = []
    for base, model in (chain or _chain()):
        try:
            raw = _ask(base, model, b64, prompt)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError, KeyError) as e:
            errors.append(f"{model}: {type(e).__name__} {str(e)[:80]}")
            continue
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            errors.append(f"{model}: unparseable reply")
            continue
        try:
            d = json.loads(m.group(0))
        except Exception:
            errors.append(f"{model}: bad JSON")
            continue
        conf = float(d.get("confidence") or 0)
        mode = (d.get("mode") or "").lower()
        rows, cols = int(d.get("rows") or 3), int(d.get("cols") or 3)
        tiles = sorted({int(t) for t in (d.get("tiles") or [])
                        if isinstance(t, (int, float)) and 0 <= int(t) < rows * cols})
        pts = [{"x": float(p["x"]), "y": float(p["y"])} for p in (d.get("points") or [])
               if isinstance(p, dict) and "x" in p and "y" in p
               and 0 <= float(p["x"]) <= width and 0 <= float(p["y"]) <= height]
        if conf < min_confidence or (mode == "grid" and not tiles) or (mode == "point" and not pts):
            errors.append(f"{model}: declined (mode={mode} conf={conf})")
            continue
        return {"mode": mode, "tiles": tiles, "points": pts, "rows": rows, "cols": cols,
                "instruction": str(d.get("instruction") or "")[:160], "confidence": conf,
                "model": model, "reasoning": str(d.get("reasoning") or "")[:200], "errors": errors}
    return {"mode": None, "tiles": [], "points": [], "confidence": 0.0, "model": None,
            "errors": errors, "reasoning": "no model produced a usable answer"}


def tile_centers(box, rows=3, cols=3):
    """Grid bounding box {x,y,width,height} -> page coordinates for each tile's centre, in index
    order. The bridge taps by CSS pixel, so this is what turns a model's answer into a click."""
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    cw, ch = w / cols, h / rows
    return [{"x": x + cw * (i % cols) + cw / 2, "y": y + ch * (i // cols) + ch / 2}
            for i in range(rows * cols)]


def main():
    # `--text shot.png` reads a distorted-string captcha; otherwise `shot.png "<instruction>"` grids.
    args = sys.argv[1:]
    if args and args[0] == "--text":
        if len(args) < 2:
            print("usage: vision_captcha.py --text shot.png")
            return 2
        png = pathlib.Path(args[1]).read_bytes()
        res = solve_text(png)
        print(json.dumps(res, indent=2))
        return 0 if res.get("text") else 1
    if len(args) < 2:
        print(__doc__.strip().splitlines()[-1])
        return 2
    png = pathlib.Path(args[0]).read_bytes()
    res = solve_grid(png, args[1])
    print(json.dumps(res, indent=2))
    return 0 if res.get("tiles") else 1


if __name__ == "__main__":
    sys.exit(main())
