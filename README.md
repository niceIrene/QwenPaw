# Scroll: Context as an Environment

This repository is a research fork of
[QwenPaw](https://github.com/agentscope-ai/QwenPaw) for **Context as an
Environment: Programmatic Context Management for Long-Horizon Agents**.

Scroll stores an agent's complete interaction history outside the model prompt
in a persistent Session Environment. The model writes Python to search,
materialize, and compute over that state; only explicitly printed projections
enter its next working context. Older turns can therefore leave the prompt
without being summarized away or becoming unrecoverable.

Compared with the upstream QwenPaw repository, this fork adds the Scroll
context manager, its append-only Event Log and persistent Python environment,
and the runtime, prompt, and CodeAct integration needed for executable context
management. Benchmark-specific adapters, task definitions, verifiers, and run
artifacts are intentionally kept out of this fork and maintained in AgentZero.

## Results

The following single-run results are reported in the accompanying paper.
Unless noted otherwise, Scroll uses Qwen3.8-Max. LongMemEval and BEAM use
Qwen3.6-Flash as the independent judge at temperature 0; LOCA uses its native
rule-based verifier.

| Benchmark | Setting | Metric | Scroll |
| --- | --- | --- | ---: |
| LongMemEval | S (~115K history tokens) | Accuracy | **94.8** |
| LongMemEval | M (~1.5M history tokens) | Accuracy | **89.6** |
| BEAM | 10M | Judge score | **73.1** |
| LOCA | 128K | Accuracy | **89.3** |
| LOCA | 256K | Accuracy | **86.7** |

| Backbone | LongMemEval-S | BEAM-10M | LOCA-128K | LOCA-256K |
| --- | ---: | ---: | ---: | ---: |
| Qwen3.8-Max | **94.8** | **73.1** | **89.3** | **86.7** |
| Qwen3.7-Max | 92.8 | 66.6 | 78.7 | 60.0 |
| DeepSeek-v4-pro | 93.2 | 70.2 | 69.3 | 58.7 |
| GLM-5.2 | 93.6 | 70.7 | 66.7 | 62.7 |
| Kimi-K2.7 | 92.0 | 67.2 | 30.7 | 32.0 |
| Qwen3.6-35B-A3B | 88.8 | 58.1 | 37.3 | 22.7 |

These values are reported results rather than values recomputed by CI. Model
serving, rate limits, and stochastic generation can affect reruns.

## Evaluation and reproduction

Evaluation is conducted with
[AgentZero](https://github.com/agentscope-ai/AgentZero), our open-source
Harbor-based evaluation framework, so benchmark-specific adapters, task
definitions, verifiers, and generated runs remain separate from the Scroll
implementation.

AgentZero pins this repository as a Git submodule and records the exact QwenPaw
commit used by an evaluation. It provides the Harbor workflows for
LongMemEval, BEAM, RULER, and LOCA, including environment construction,
parallel trials, traces, and scoring.

```bash
git clone --recurse-submodules https://github.com/agentscope-ai/AgentZero.git
cd AgentZero

uv python install 3.12
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install "harbor==0.18.0" "ijson>=3.3.0"
```

Build the pinned Scroll/QwenPaw wheel, then generate a small LongMemEval task
set:

```bash
uv build --project qwenpaw --wheel --out-dir dist
export QWENPAW_WHEEL="$(find "$PWD/dist" -name 'qwenpaw-*.whl' -print -quit)"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

python scripts/download_longmemeval_data.py --dataset oracle
python benchmarks/longmemeval/generate.py \
  benchmarks/longmemeval/data/longmemeval_oracle.json \
  --split smoke \
  --output local-tasks/longmemeval \
  --limit 3
```

Supply your own evaluated-model and judge API keys, base URLs, and model
identifiers, then launch Harbor:

```bash
harbor run \
  --job-name longmemeval-scroll-smoke \
  -p local-tasks/longmemeval/smoke \
  -a adapters.qwenpaw.longmemeval:QwenPawLongMemEvalAgent \
  -m YOUR_PROVIDER_ID/YOUR_MODEL_ID \
  --ae QWENPAW_WHEEL="$QWENPAW_WHEEL" \
  --ae QWENPAW_MODEL_API_KEY="$QWENPAW_MODEL_API_KEY" \
  --ae QWENPAW_MODEL_BASE_URL="$QWENPAW_MODEL_BASE_URL" \
  --ve LONGMEMEVAL_JUDGE_API_KEY="$LONGMEMEVAL_JUDGE_API_KEY" \
  --ve LONGMEMEVAL_JUDGE_BASE_URL="$LONGMEMEVAL_JUDGE_BASE_URL" \
  --ve LONGMEMEVAL_JUDGE_MODEL="$LONGMEMEVAL_JUDGE_MODEL" \
  -n 3
```

See the AgentZero README for Oracle validation, full S/M runs, other
benchmarks, concurrency guidance, and result inspection.

## License

The implementation is released under the repository's [Apache 2.0](LICENSE)
license. Third-party benchmark data and evaluation components remain subject
to their own licenses.

## Citation

If you use Scroll or this artifact, please cite:

```bibtex
@techreport{lin2026context,
  title       = {Context as an Environment: Programmatic Context Management for Long-Horizon Agents},
  author      = {Lin, Yin and Ang, Elaine and Zhu, Erkang and Ding, Bolin and Zhou, Jingren},
  year        = {2026},
  institution = {Alibaba Group and Columbia University},
  note        = {Technical Report}
}
```
