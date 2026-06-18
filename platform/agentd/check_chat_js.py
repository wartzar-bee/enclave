#!/usr/bin/env python3
"""
check_chat_js.py — build-time guard for the chat UI's inline JavaScript.

web_chat.PAGE is a NON-raw triple-quoted Python string, so every backslash in
the embedded JS (regex `\\n`, `\\s`, `\\d`, `\\u0000`, …) must be doubled. A single
backslash silently becomes a control char in the rendered string and breaks the
script at runtime — twice this has shipped a dead send button. This script
renders PAGE exactly as the browser receives it, extracts every <script> block,
and runs `node --check` on each so a JS syntax error fails the build, not a user.

Usage:  python3 check_chat_js.py
Exit:   0 = all script blocks parse · 1 = a syntax error · 2 = setup problem
        If `node` is not on PATH, prints a warning and exits 0 (non-blocking).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def main():
    node = shutil.which("node")
    if not node:
        print("check_chat_js: node not found on PATH — skipping JS syntax check "
              "(non-blocking; the image bakes node, host check skipped)")
        return 0

    sys.path.insert(0, HERE)
    try:
        import web_chat  # noqa: E402  (server only starts under __main__)
    except Exception as e:  # pragma: no cover
        print(f"check_chat_js: could not import web_chat: {e}", file=sys.stderr)
        return 2

    # Render PAGE exactly as the browser gets it. __SPARK__ is already substituted
    # at module load; __NAME__ is substituted per-request — use a placeholder.
    html = web_chat.PAGE.replace("__NAME__", "Agent")

    blocks = SCRIPT_RE.findall(html)
    if not blocks:
        print("check_chat_js: no <script> blocks found in PAGE", file=sys.stderr)
        return 2

    failures = 0
    for i, js in enumerate(blocks):
        js = js.strip()
        if not js:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
            path = f.name
        try:
            r = subprocess.run([node, "--check", path],
                               capture_output=True, text=True)
        finally:
            os.unlink(path)
        if r.returncode != 0:
            failures += 1
            print(f"check_chat_js: SYNTAX ERROR in <script> block #{i + 1}:",
                  file=sys.stderr)
            print(r.stderr.strip(), file=sys.stderr)

    if failures:
        print(f"check_chat_js: FAILED — {failures} script block(s) have JS "
              f"syntax errors (see above). Fix web_chat.PAGE before deploying.",
              file=sys.stderr)
        return 1

    print(f"check_chat_js: OK — {len(blocks)} script block(s) parse cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
