# QwenPaw × BEAM on Harbor

中文说明见 [README.zh-CN.md](README.zh-CN.md)。

This directory packages ten BEAM 10M conversations as Harbor 1.3 tasks. Each
task keeps public inputs in `environment/`, hidden rubrics in `tests/`, and the
reference output in `solution/` for Oracle validation.

The QwenPaw runner imports the full chat into Scroll history once, asks every
probe in an isolated session, checkpoints `answers.json` after each probe, and
writes per-probe traces and token/recall metrics to Harbor's agent logs.

## What was adapted

This integration does not change the semantics of the regular
`qwenpaw task -i` command. It adds a purpose-built Harbor/BEAM evaluation path:

- `benchmark_adapters/qwenpaw_beam_agent.py` is the custom installed-agent
  adapter on the Harbor host. It copies the wheel built from the current
  checkout into the task container, preserves its valid wheel filename,
  installs it, initializes an isolated workspace, bridges Harbor's
  `provider/model` and API credentials, and starts the BEAM runner.
- `src/qwenpaw/evals/beam_runner.py` streams the large `chat.json` into Scroll's
  `history.db` and then executes 20 probes. Ingestion bypasses ReMe
  summarization and does not generate headlines. Every probe uses a distinct
  session, so its answer and tool context do not enter the next probe.
- Imported rows use the dedicated `kind="beam_chat_turn"`. Probe prompts require
  recall to filter on that kind, preventing questions, answers, or tool results
  from earlier probes in the shared database from becoming BEAM evidence.
- `benchmark_adapters/beam_judge.py` provides official-compatible scoring. It
  assigns `0/0.5/1` per rubric criterion and aggregates by question and
  ability; event ordering uses semantic alignment and normalized Kendall tau;
  the Harbor reward is the macro mean of all ten ability scores.
- `harbor/beam/10M-1` through `10M-10` package the ten conversations as Harbor
  1.3 tasks. `environment/` contains agent-visible input, `tests/` contains the
  verifier rubric and entry point, and `solution/` is used only for the Oracle
  contract check.
- `tests/unit/evals/` covers streaming JSON, history ingestion, probe isolation,
  judge parsing, rubric aggregation, and event-ordering calculations.

The complete execution path is:

```text
Harbor trial
  → install the current QwenPaw wheel
  → stream one BEAM conversation into Scroll
  → answer 20 probes sequentially in isolated sessions
  → checkpoint answers, metrics, and per-probe traces
  → run the LLM verifier and write reward/scores
```

Parallelism is applied across independent Harbor trials and rubric-judge calls.
The 20 probes within one task remain sequential to avoid contention in the
shared workspace, runtime, and SQLite database.

## 1. Prerequisites

- Python 3.11–3.13 for QwenPaw
- Harbor and a working Docker daemon
- An API key for the QwenPaw model
- A separate API key for the rubric judge

Install Harbor and build a wheel containing the current checkout:

```bash
uv tool install harbor
uv build --wheel --out-dir dist
export QWENPAW_WHEEL="$(find "$PWD/dist" -name 'qwenpaw-*.whl' -print -quit)"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

The explicit wheel is important: the custom BEAM runner must be the version
from this checkout, not an older package from PyPI. The repository root must
also be on `PYTHONPATH`: Harbor installed through `uv tool` runs in an isolated
Python environment and otherwise cannot import the local custom
`benchmark_adapters` agent.

## 2. Configure the judge

The verifier uses an OpenAI-compatible endpoint and scores each rubric item as
0, 0.5, or 1. It then averages rubric scores per question and question scores
for the final reward.

```bash
export BEAM_JUDGE_API_KEY="..."
export BEAM_JUDGE_BASE_URL="https://api.openai.com/v1"
export BEAM_JUDGE_MODEL="gpt-4.1-mini"
```

For DashScope, use its OpenAI-compatible base URL and an available Qwen judge
model instead. The report records both the judge model and the
`beam-official-compatible-v1` judge variant. It uses BEAM's official unified
rubric prompt and category aggregation. For event ordering it uses normalized
Kendall tau, as the official report code does; semantic event alignment is
batched into one judge call per question to avoid the official implementation's
large number of pairwise calls. This port also fills the question placeholder
left unresolved by the current upstream call site and preserves 0.5 scores
rather than truncating them through an integer cast.

## 3. Validate the task contract

First run one reference solution. This checks the container layout, answer
schema, judge access, and reward generation without paying for a QwenPaw run:

```bash
harbor run -p harbor/beam/10M-1 -a oracle
```

## 4. Run one QwenPaw trial

Example using DashScope as the evaluated model:

```bash
harbor run \
  -p harbor/beam/10M-1 \
  -a benchmark_adapters.qwenpaw_beam_agent:QwenPawBeamAgent \
  -m dashscope/qwen3.7-max \
  --ae QWENPAW_WHEEL="$QWENPAW_WHEEL" \
  --ae DASHSCOPE_API_KEY="$DASHSCOPE_API_KEY"
```

For OpenAI, select an `openai/<model>` identifier and pass
`--ae OPENAI_API_KEY=...`. For a custom OpenAI-compatible endpoint, also pass
`--ae QWENPAW_MODEL_API_KEY=...` and
`--ae QWENPAW_MODEL_BASE_URL=...`; the provider segment before `/` must still
match a QwenPaw provider ID.

By default the adapter uses the structured `recall_history` tool. To evaluate
the sandboxed Python recall interface instead, add:

```bash
--ae QWENPAW_BEAM_RECALL_TOOL=python
```

## 5. Run all ten conversations

After the single-task smoke test succeeds:

```bash
harbor run \
  -p harbor/beam \
  -a benchmark_adapters.qwenpaw_beam_agent:QwenPawBeamAgent \
  -m dashscope/qwen3.7-max \
  --ae QWENPAW_WHEEL="$QWENPAW_WHEEL" \
  --ae DASHSCOPE_API_KEY="$DASHSCOPE_API_KEY" \
  -n 2 \
  --n-concurrent-agents 2
```

Here `-n 2` allows Harbor to run at most two independent trials at once, while
`--n-concurrent-agents 2` applies the same cap specifically to agent execution.
Each trial has its own Docker container, QwenPaw workspace, and `history.db`, so
trial-level parallelism is the recommended approach.

The concurrency layers are different:

- The 20 probes inside one `10M-*` task remain sequential to avoid runtime-state
  and SQLite-write contention in a shared workspace.
- Rubric judging already runs up to four API calls concurrently by default;
  configure this with `BEAM_JUDGE_CONCURRENCY`.
- `-p harbor/beam/10M-1` creates only one trial, so `-n 2` or `-n 4` cannot make
  its 20 internal probes parallel.
- `-p harbor/beam` discovers all ten tasks, so `-n 2` runs two of those trials
  concurrently.

For the first full run, use `-n 1 --n-concurrent-agents 1`. Increase it to 2
only after checking local memory and disk capacity, evaluated-model and judge
API limits, and cost. Every trial installs QwenPaw, ingests roughly 20,000
messages, makes 20 answer calls, and then makes rubric-judge calls.

To validate Oracle tasks in parallel without running QwenPaw:

```bash
harbor run \
  -p harbor/beam \
  -a oracle \
  -n 2
```

Judge concurrency is independent of Harbor trial concurrency. It defaults to
four and can be configured in the environment:

```bash
export BEAM_JUDGE_CONCURRENCY=4
```

If Harbor reports:

```text
Failed to import module 'benchmark_adapters.qwenpaw_beam_agent':
No module named 'benchmark_adapters'
```

the current shell has not exposed the repository root to Harbor. From the
QwenPaw repository root, run:

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

Then retry the `harbor run` command. This variable must be exported to the
Harbor host process because Harbor imports the adapter before creating the
Docker environment.

## Outputs and resumability

- `/app/answers.json`: verifier input, checkpointed after every probe
- `/logs/agent/metrics.json`: ingestion, latency, token, recall-call, and error
  metrics
- `/logs/agent/traces/<probe-id>.json`: raw QwenPaw event/tool trace per probe
- `/logs/verifier/scores.json`: rubric, question, and category scores
- `/logs/verifier/reward.txt`: Harbor scalar reward

Harbor downloads `/logs` with each trial. A Harbor retry starts a clean trial;
inside a running trial, completed probes are always preserved in
`answers.json` and the logs even if a later probe fails.

## Why this does not use `qwenpaw task -i`

`qwenpaw task -i` is useful for one free-form instruction, but BEAM needs a
controlled multi-stage protocol: bulk history import, 20 isolated probe
sessions, structured answer aggregation, and per-probe tracing. The adapter
therefore calls `python -m qwenpaw.evals.beam_runner`. It still exercises the
same QwenPaw workspace, model provider, Scroll history, and recall tools.

## Data provenance

The task data comes from the BEAM benchmark. Retain the upstream dataset's
CC BY-SA 4.0 attribution and share-alike requirements when redistributing the
generated task data. The BEAM code repository is MIT-licensed.
See [DATA_LICENSE.md](DATA_LICENSE.md) for attribution and source links.
