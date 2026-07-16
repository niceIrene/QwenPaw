# 用 Harbor 在 BEAM 上评测 QwenPaw

这里已经把 BEAM 的 10 个 10M 对话整理成 Harbor 1.3 task。每个 task
包含公开输入 `environment/`、隐藏 rubric `tests/` 和只供 Oracle 校验的
参考答案 `solution/`。

正式评测不需要执行 `qwenpaw task -i`。BEAM 不是单条 instruction，而是
“导入长对话 → 隔离运行 20 个 probe → 聚合答案 → LLM rubric 打分”的协议，
因此 Harbor adapter 会调用专用的 `qwenpaw.evals.beam_runner`。底层仍然使用
真实的 QwenPaw workspace、模型 provider、Scroll history 和 recall 工具。

## 1. 准备环境

需要 Harbor、Docker、被测 QwenPaw 模型的 API key，以及一个独立的 judge
模型 API key。先在 QwenPaw 仓库根目录执行：

```bash
uv tool install harbor
uv build --wheel --out-dir dist
export QWENPAW_WHEEL="$(find "$PWD/dist" -name 'qwenpaw-*.whl' -print -quit)"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

必须使用当前 checkout 构建的 wheel，因为公开版 PyPI 包未必包含这里新增的
BEAM runner。`PYTHONPATH` 也必须包含仓库根目录：通过 `uv tool` 安装的
Harbor 运行在独立 Python 环境中，否则它无法导入仓库里的自定义
`benchmark_adapters` agent。

## 2. 配置 judge

```bash
export BEAM_JUDGE_API_KEY="..."
export BEAM_JUDGE_BASE_URL="https://api.openai.com/v1"
export BEAM_JUDGE_MODEL="gpt-4.1-mini"
```

也可以改成 DashScope 的 OpenAI-compatible 地址和可用的 Qwen judge 模型。
Judge 与被测模型最好使用不同的 key，便于独立统计成本和限流。当前评测器
标记为 `beam-official-compatible-v1`：使用 BEAM 官方统一 rubric prompt 和
类别聚合方式；`event_ordering` 按官方 report 代码使用归一化 Kendall tau。
为控制 API 调用量，事件语义对齐由官方的逐对调用改为每题一次批量调用。
此外，本实现会填充上游当前调用点未替换的 question placeholder，并保留
rubric prompt 定义的 0.5 分，而不是通过整数转换把 0.5 截断为 0。

## 3. 先验证一个 Oracle task

```bash
harbor run -p harbor/beam/10M-1 -a oracle
```

这一步不运行 QwenPaw，只验证容器目录、参考答案、judge 和 reward 文件。

## 4. 跑一个 QwenPaw task

下面以 DashScope 为例：

```bash
harbor run \
  -p harbor/beam/10M-1 \
  -a benchmark_adapters.qwenpaw_beam_agent:QwenPawBeamAgent \
  -m dashscope/qwen3.7-max \
  --ae QWENPAW_WHEEL="$QWENPAW_WHEEL" \
  --ae DASHSCOPE_API_KEY="$DASHSCOPE_API_KEY"
```

如果用 OpenAI，则选择 `openai/<model>` 并传入
`--ae OPENAI_API_KEY=...`。若是 OpenAI-compatible 自定义地址，可传入：

```bash
--ae QWENPAW_MODEL_API_KEY="..." \
--ae QWENPAW_MODEL_BASE_URL="https://your-endpoint/v1"
```

模型参数的 provider 部分必须是 QwenPaw 已知的 provider ID。默认用结构化
`recall_history`；如需测试 Python recall，增加：

```bash
--ae QWENPAW_BEAM_RECALL_TOOL=python
```

## 5. 跑完整的 10 个 task

单 task 成功后，再执行：

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

这里的 `-n 2` 表示 Harbor 最多同时运行 2 个独立 trial，
`--n-concurrent-agents 2` 把 agent 执行阶段也限制为最多 2 个。每个 trial
拥有独立的 Docker 容器、QwenPaw workspace 和 `history.db`，因此这是推荐的
并行层级。

并发行为需要区分：

- 单个 `10M-*` task 内的 20 个 probe 仍然串行执行，避免共享 workspace 的
  runtime 状态和 SQLite 写入发生竞争。
- Judge rubric 默认最多并行 4 个 API 请求；可通过
  `BEAM_JUDGE_CONCURRENCY` 调整。
- `-p harbor/beam/10M-1` 只有一个 trial，因此设置 `-n 2` 或 `-n 4` 不会
  让该 task 内的 20 个 probe 并行。
- `-p harbor/beam` 会发现 10 个 task，此时 `-n 2` 才会并行运行其中两个。

建议第一次完整运行先用 `-n 1 --n-concurrent-agents 1`，确认内存、磁盘、
被测模型和 Judge API 的限流及费用后，再提高到 2。每个 task 约导入 2 万条
消息、执行 20 次被测模型调用，之后还会执行多次 rubric Judge 调用。

只想并行验证 Oracle 时，可以使用：

```bash
harbor run \
  -p harbor/beam \
  -a oracle \
  -n 2
```

Judge 自身的并发与 Harbor trial 并发相互独立；默认是 4，可以在环境中设置：

```bash
export BEAM_JUDGE_CONCURRENCY=4
```

如果出现下面的错误：

```text
Failed to import module 'benchmark_adapters.qwenpaw_beam_agent':
No module named 'benchmark_adapters'
```

说明当前 shell 没有把仓库根目录传给 Harbor。请回到 QwenPaw 仓库根目录并
执行：

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

然后直接重新执行上面的 `harbor run`。这个变量必须 export 给 Harbor 主进程，
因为 adapter 是在创建 Docker 容器之前由 Harbor 本身导入的。

## 结果在哪里

- `/app/answers.json`：每个 probe 完成后立即 checkpoint 的答案
- `/logs/agent/metrics.json`：导入耗时、probe 耗时、token 和 recall 次数
- `/logs/agent/traces/<probe-id>.json`：QwenPaw 原始事件和工具轨迹
- `/logs/verifier/scores.json`：rubric、问题和类别分数
- `/logs/verifier/reward.txt`：Harbor 最终标量 reward

Harbor 会把 `/logs` 下载到 trial 结果目录。数据来源与 CC BY-SA 4.0
要求见 [DATA_LICENSE.md](DATA_LICENSE.md)。
