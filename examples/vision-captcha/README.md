# vision-captcha — a bring-your-own-model CAPTCHA reader

An enclave agent that drives a browser eventually hits a CAPTCHA. You already pay for a vision model
that can see; a CAPTCHA is a vision task. This single-file, stdlib-only tool reads the challenge with
*your own* model instead of a per-challenge classifier or a CapSolver/2Captcha subscription.

- **Vendor-free.** Sends the crop straight to *your* Anthropic subscription/key, or any OpenAI-compat
  vision endpoint. Never routes Claude through a reseller.
- **General, not trained.** A new challenge wording ("click the animals facing left") needs no
  retraining — it asks a general vision model in plain language.
- **Zero dependencies.** One file, Python stdlib only (`urllib`, `base64`, `json`). Drop it next to
  your agent code.

## Modes

| function | challenge | returns |
|---|---|---|
| `solve_grid(png, instruction)` | hCaptcha 3×3 image tiles | tile indices to tap |
| `solve_point(png, instruction, w, h)` | click-the-point | page coordinates |
| `solve_text(png)` | distorted-text (Gitea/Forgejo-style) | the transcribed string |
| `solve_text_vote(png, min_agree=2)` | distorted-text, high-precision | string only on quorum, else decline |
| `solve_auto(png, w, h)` | dispatches by challenge shape | mode-appropriate result |

## Bring your own model (quickstart)

Credentials resolve from the **environment first** (no repo layout required). Point it at whatever you
already pay for:

```bash
# Option A — your Anthropic subscription/key (Claude-direct, recommended)
export ANTHROPIC_API_KEY=<your-key>          # or CLAUDE_CODE_OAUTH_TOKEN

# Option B — any OpenAI-compat vision endpoint (OpenRouter, NVIDIA NIM, a local server…)
export VISION_CAPTCHA_API_KEY=<your-key>
export VISION_CAPTCHA_MODELS='{"base":"https://integrate.api.nvidia.com/v1","model":"meta/llama-3.2-90b-vision-instruct"}'

python3 vision_captcha.py --text challenge.png     # transcribe distorted text
python3 vision_captcha.py --grid challenge.png "click every image with a bus"
```

The model chain is tried in order and falls back on error; a low-confidence read **declines** (returns
`None`) rather than guessing — so you can retry or escalate instead of submitting a wrong answer.

## Measuring accuracy — honestly

Solve rate depends entirely on **your** model and challenge set, so this ships **no claimed accuracy**.
Measure it yourself: run a mode over a labelled set where the filename is the ground truth and count
exact matches. As one reproducible data point, on the public
[`captcha_images_v2`](https://github.com/AakashKumarNain/CaptchaCracker) distorted-text set
(filename = truth) a `meta/llama-3.2-90b-vision-instruct` reader scored **75/100 exact (75%)** in our
internal n=100 run (2026-07-26; an earlier n=25 gave 19/25=76%, confirmed at 4× the sample) — a number
you can reproduce with the command above and your own key. Your mileage will vary by model; that is the
point of bring-your-own-model.

For a higher-precision mode, `solve_text_vote` polls several models and acts only on agreement (a
principled decline otherwise) — trading coverage for precision on hard challenges.

## Scope & honesty

- This is a **reader**, not a bypass: it transcribes/locates what a human would, using a general model.
- It respects the guard model of enclave — it computes an answer; *your* agent decides whether to submit.
- No accuracy is asserted without a measurement you can rerun. See the hard rule above.

---

Part of the **[wartzar-bee](https://github.com/wartzar-bee)** cost-efficient-agent toolkit alongside
[`tokenscope`](https://www.npmjs.com/package/@wartzar-bee/tokenscope) (per-turn token/cost accounting)
and [`ci-guardrail`](https://github.com/wartzar-bee/ci-guardrail) (block token-cost regressions in CI).
Apache-2.0, same as enclave.
