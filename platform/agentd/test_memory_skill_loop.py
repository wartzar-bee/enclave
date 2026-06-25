#!/usr/bin/env python3
"""
test_memory_skill_loop.py — self-tests for the P3 SKILL learned-memory loop (Hermes borrow).

Covers the two halves the loop was missing before P3:
  (A) QUALITY GATE on learn() — reject thin / non-procedural skills; admit good ones (fail-open).
  (B) RELOAD — a learned skill is surfaced back into the pre-tick digest when relevant.

No network / no node: the gate's local-LLM dedup step FAILS OPEN (route.mjs absent here), so a
genuinely-new, well-formed skill is admitted and tagged `unverified`. Deterministic, hermetic.

Run:  python3 platform/agentd/test_memory_skill_loop.py   (exit 0 = all pass)
"""
import sys, tempfile, pathlib
from memory import Memory

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok  {name}")
    else: FAIL += 1; print(f"FAIL  {name}")

def main():
    with tempfile.TemporaryDirectory() as d:
        m = Memory(d)

        # ── (A) QUALITY GATE ────────────────────────────────────────────────
        # too thin → rejected, nothing written
        f, reason = m.learn("thin-skill", "Thin", "do a thing", gate=True)
        check("gate rejects <120-char body", f is None and "thin" in reason.lower())
        check("rejected skill not written", not (pathlib.Path(d) / "skills" / "thin-skill.md").exists())

        # long but a bare fact (single line, no steps) → rejected as non-procedural
        bare = "The production deploy branch for tokenscope Cloudflare Pages is main, not master, " \
               "which is a fact worth knowing but is not itself a reusable how-to procedure at all."
        f, reason = m.learn("bare-fact", "Bare fact", bare, gate=True)
        check("gate rejects non-procedural body", f is None and "procedural" in reason.lower())

        # first good skill, no prior skills to dedup against → admitted, tagged `verified`
        good = ("How to publish a still-image YouTube video for the audio channel:\n"
                "1. Render the cover PNG at 1920x1080 with ffmpeg loop=1.\n"
                "2. Mux the mp3 with -shortest so length tracks the audio.\n"
                "3. Upload via the API with the SEO title/desc/tags from show.md.\n"
                "4. Add the new video to the serial playlist.")
        f, ver = m.learn("render-youtube-stillvideo", "Render a still-image YouTube video", good, gate=True)
        check("gate admits a well-formed procedure", f is not None and ver == 1)
        check("first skill (nothing to dedup) tagged verified", "gated: verified" in f.read_text())
        check("skill listed in skills INDEX", "render-youtube-stillvideo.md" in
              (pathlib.Path(d) / "skills" / "INDEX.md").read_text())

        # second, DISTINCT skill — now a prior skill exists → LLM dedup runs → route.mjs absent
        # here → FAIL OPEN → admitted but tagged `unverified` (never blocks on infra).
        other = ("How to seed a venture in a high-intent community without spamming:\n"
                 "1. Find one thread where the problem is being actively discussed.\n"
                 "2. Lead with a substantive answer; mention the tool only if it fits.\n"
                 "3. Log the post + UTM and watch the funnel for a pull signal.")
        f4, _ = m.learn("community-seed-no-spam", "Seed a community without spam", other, gate=True)
        check("2nd skill admitted via fail-open dedup", f4 is not None and "gated: unverified" in f4.read_text())

        # ungated learn still works unchanged (back-compat)
        f2, v2 = m.learn("legacy-skill", "Legacy", "x")
        check("ungated learn keeps writing (back-compat)", f2 is not None and "gated:" not in f2.read_text())

        # re-learning a slug re-versions it
        f3, v3 = m.learn("render-youtube-stillvideo", "Render a still-image YouTube video",
                         good + "\n5. Pin the playlist to the channel home.", gate=True)
        check("re-learning same slug bumps version", v3 == 2)

        # ── (B) RELOAD into the digest ──────────────────────────────────────
        ranked = m._rank_skills("youtube still image video upload playlist", k=2)
        check("relevant skill ranks for its query", ranked and ranked[0][1] == "render-youtube-stillvideo")
        check("irrelevant query ranks nothing", m._rank_skills("quantum chromodynamics lattice") == [])

        dg = m.digest(query="render a youtube still-image video and add to the playlist")
        check("digest surfaces the Learned skills section", "## Learned skills" in dg)
        check("digest names the relevant skill", "render-youtube-stillvideo.md" in dg)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
