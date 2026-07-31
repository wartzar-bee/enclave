# EVAL — benchmark models on any pool endpoint

`enclave eval` measures a model's real capability on the pools this deployment can route to, so
model picks in `policy.json`/the catalog cite evidence instead of vibes.

## Quick start
```bash
enclave eval capability --models mlx-community/Qwen3-8B-4bit --pool mlx
enclave eval gsm8k --models m1,m2 --pool nvidia --n 50 --record
enclave eval gsm8k --models my-model --base http://localhost:8081/v1 --data rows.json
```
- **`--pool`** resolves from a `policy.json` `pools` section (`$LLM_POLICY` or `--policy`) or the
  console catalog `providers` store; **`--base`** hits any OpenAI-compatible endpoint directly.
- Adapters: **`capability`** (6-task battery: reasoning/coding/json/extraction/classification/
  instruction) · **`gsm8k`** (external math, exact-match; rows via `--data` or the no-install
  HF datasets-server fetch). Vision + tool-calling adapters are planned.
- Per-row results → jsonl (`--out`, default `$AGENT_DIR/state/evals/`); per-model summaries →
  stdout; `--record` appends them to the catalog evidence trail (`eval_results`, last 10 per model).

## Params are documented, never guessed
Each model runs at its **model-card** sampling params + thinking mode from the catalog
`model_params` store (`catalog.set_model_params` / console). A single global temperature invalidates
comparisons — models without an entry fall back to defaults and their results are tagged
`params_source:"default"`, so an undocumented run is never mistaken for a documented one.
Thinking is requested via `chat_template_kwargs.enable_thinking`; endpoints that reject extra
fields get one retry with extras stripped (flagged `extras_stripped` in the rows).

## Design notes
- Models run **sequentially** — a local single-resident server (MLX) evicts on model switch; never
  make it hold two. For Metal-direct local runs (bypassing the server), keep a host-side harness
  using one process per model; this runner is deliberately endpoint-only and dependency-free.
- Grading strips reasoning traces first (qwen/gemma/nemotron trace styles) and grades the FINAL
  answer; an unclosed trace is counted as `truncated` (the token budget cut it off).
- Adapter = `name` + `tasks(opts)` + `grade(task, text)` + `summarize(rows)`
  (`platform/agentd/eval/adapters.py`). Add a third adapter before inventing any plugin SPI.
