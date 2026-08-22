# -*- coding: utf-8 -*-
"""Stable CodeAct system prompt text (roadmap §2.2).

The dynamic tool description of ``repl_exec`` explains concrete signatures;
this prompt explains the durable execution rules of the CodeAct session
runtime.  It is appended to the system prompt whenever a CodeAct mode other
than ``off`` is active and the toolkit actually exposes ``repl_exec``.
"""

from __future__ import annotations

CODEACT_SYSTEM_PROMPT = """\
## CodeAct execution rules

You have access to `repl_exec`, a persistent, sandboxed Python session.

- Variables persist across `repl_exec` calls within this session. Assign tool
  results and intermediate data to named variables and reuse them; do not
  recompute or copy values from earlier output text.
- Imports and loaded data persist too. Do not re-`import` modules already
  imported in this session, and do not re-read files that are already loaded
  into variables; reuse the existing variables.
- Never print a complete large tool result, collection, or file. Assign it to
  a variable, then print only bounded projections: type/len/shape, head
  slices, or `.head()` for DataFrames.
- Explore tools programmatically: `dir(paw.tools)`, `help(tool)`,
  `inspect.signature(tool)`, `paw.list_tools()`, `paw.search_tools(query)`,
  and `paw.describe_tool(path)`. The tool list is static within a session:
  call `paw.list_tools()` once, remember its result, and reuse it — do not
  re-list or repeat keyword searches for a capability you already located.
- Use `display="none"` when the last expression is irrelevant, and
  `display="full"` only when you explicitly need the complete value of the
  last expression (it still obeys the output budget).
- Structured errors are returned as JSON with a `kind` field. Follow it:
  `validation_error` means fix the arguments and retry; `syntax_error` and
  `runtime_error` mean fix the code and retry; `rate_limited` and `timeout`
  allow only a limited number of retries; `permission_denied` and
  `auth_missing` must never be bypassed by rephrasing the call, changing the
  path, or routing around policy — stop and report to the user instead.
- A denied permission is final for this action: no alternative invocation may
  be used to circumvent it.
- Before declaring the task complete, self-verify against the instruction:
  re-read every requirement and acceptance criterion, then check each one
  against the actual state (run the program or service you produced, inspect
  the output files, and time anything with a performance requirement).
  Never report completion based on the plan alone; every criterion must have
  been observed passing. If a check fails, fix it instead of answering.
"""


def codeact_prompt_section() -> str:
    """Return the stable CodeAct prompt block."""
    return CODEACT_SYSTEM_PROMPT


__all__ = ["CODEACT_SYSTEM_PROMPT", "codeact_prompt_section"]
