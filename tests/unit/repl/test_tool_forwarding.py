"""Main-process tool schema forwarding tests."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from qwenpaw.repl.governance_bridge import (
    GovernanceBridge,
    ToolForwardingError,
    build_tool_specs,
    get_code_provenance,
)
from qwenpaw.repl.tool_def import make_repl_only_toolkit


class FakeToolkit:
    def __init__(self, schemas: list[dict], tools: list[object]) -> None:
        self._schemas = schemas
        self.tool_groups = [SimpleNamespace(tools=tools)]

    async def get_tool_schemas(self, _groups):
        return self._schemas


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} description",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.mark.asyncio
async def test_mcp_tools_use_server_namespace_and_exclusions() -> None:
    capability = SimpleNamespace(
        protocol="mcp",
        driver_name="my-server",
        name="find.items",
    )
    mcp_tool = SimpleNamespace(
        name="display__find_items", _capability=capability
    )
    repl = SimpleNamespace(name="repl_exec")
    toolkit = FakeToolkit(
        [_schema("display__find_items"), _schema("repl_exec")],
        [mcp_tool, repl],
    )
    state = SimpleNamespace(tool_context=SimpleNamespace(activated_groups=[]))

    specs = await build_tool_specs(toolkit, state)

    assert [item["path"] for item in specs] == [
        "mcp.my_server.find_items",
    ]


@pytest.mark.asyncio
async def test_sanitized_name_collision_fails_closed() -> None:
    tools = [
        SimpleNamespace(name="a-b"),
        SimpleNamespace(name="a.b"),
    ]
    toolkit = FakeToolkit([_schema("a-b"), _schema("a.b")], tools)
    state = SimpleNamespace(tool_context=SimpleNamespace(activated_groups=[]))

    with pytest.raises(ToolForwardingError, match="collision"):
        await build_tool_specs(toolkit, state)


@pytest.mark.asyncio
async def test_real_toolkit_roundtrip_preserves_code_provenance(
    tmp_path,
) -> None:
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.state import AgentState
    from agentscope.tool import FunctionTool, ToolChunk, Toolkit

    observed = {}

    async def lookup(query: str) -> ToolChunk:
        provenance = get_code_provenance()
        observed["provenance"] = provenance
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[
                TextBlock(
                    type="text",
                    text=f'{{"query": "{query}", "count": 2}}',
                ),
            ],
        )

    toolkit = Toolkit(tools=[FunctionTool(lookup)])
    state = AgentState()
    specs = await build_tool_specs(toolkit, state)
    bridge = GovernanceBridge(
        toolkit,
        state,
        tmp_path,
        "workspace-1",
        specs,
    )

    value = await bridge.dispatch(
        "lookup",
        {"query": "revenue"},
        kernel_task_id="exec-1",
    )

    assert value == {"query": "revenue", "count": 2}
    assert observed["provenance"].provenance == "code"
    assert observed["provenance"].workspace_id == "workspace-1"
    assert observed["provenance"].kernel_task_id == "exec-1"


@pytest.mark.asyncio
async def test_repl_only_model_toolkit_keeps_hidden_tool_forwarding(
    tmp_path,
) -> None:
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.state import AgentState
    from agentscope.tool import FunctionTool, ToolChunk, Toolkit

    async def repl_exec(code: str) -> ToolChunk:
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[TextBlock(type="text", text=code)],
        )

    async def hidden_lookup(query: str) -> ToolChunk:
        return ToolChunk(
            state=ToolResultState.SUCCESS,
            content=[TextBlock(type="text", text=f'{{"query": "{query}"}}')],
        )

    full_toolkit = Toolkit(
        tools=[FunctionTool(repl_exec), FunctionTool(hidden_lookup)],
    )
    model_toolkit = make_repl_only_toolkit(full_toolkit)
    state = AgentState()

    model_schemas = await model_toolkit.get_tool_schemas([])
    assert [item["function"]["name"] for item in model_schemas] == [
        "repl_exec",
    ]

    specs = await build_tool_specs(full_toolkit, state)
    assert [item["name"] for item in specs] == ["hidden_lookup"]
    bridge = GovernanceBridge(
        full_toolkit,
        state,
        tmp_path,
        "workspace-1",
        specs,
    )
    assert await bridge.dispatch(
        "hidden_lookup",
        {"query": "kept"},
        kernel_task_id="exec-1",
    ) == {"query": "kept"}


@pytest.mark.asyncio
async def test_real_toolkit_denial_becomes_actionable_error(tmp_path) -> None:
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.state import AgentState
    from agentscope.tool import FunctionTool, ToolChunk, Toolkit

    async def guarded_action(target: str) -> ToolChunk:
        return ToolChunk(
            state=ToolResultState.DENIED,
            content=[
                TextBlock(type="text", text=f"blocked target {target}"),
            ],
        )

    toolkit = Toolkit(tools=[FunctionTool(guarded_action)])
    state = AgentState()
    specs = await build_tool_specs(toolkit, state)
    bridge = GovernanceBridge(
        toolkit,
        state,
        tmp_path,
        "workspace-1",
        specs,
    )

    with pytest.raises(ToolForwardingError, match="denied.*bypass"):
        await bridge.dispatch(
            "guarded_action",
            {"target": "/outside"},
            kernel_task_id="exec-1",
        )


def test_excluded_tool_points_to_the_structured_channel(tmp_path) -> None:
    bridge = GovernanceBridge(
        FakeToolkit([], []),
        SimpleNamespace(),
        tmp_path,
        "workspace-1",
        [],
    )

    with pytest.raises(ToolForwardingError, match="structured tool"):
        bridge._resolve_tool(
            "execute_python"
        )  # pylint: disable=protected-access


def test_binary_content_is_spilled_to_the_workspace(tmp_path) -> None:
    bridge = GovernanceBridge(
        FakeToolkit([], []),
        SimpleNamespace(),
        tmp_path,
        "workspace-1",
        [],
    )
    payload = b"\x89PNG\r\n\x1a\nfake"
    block = SimpleNamespace(
        type="image",
        source=SimpleNamespace(
            media_type="image/png",
            data=base64.b64encode(payload).decode("ascii"),
        ),
    )

    value = bridge._convert_content(  # pylint: disable=protected-access
        [block],
        "call-1",
    )

    assert value == {
        "path": "out/tool_call-1_0.png",
        "media_type": "image/png",
    }
    assert (tmp_path / value["path"]).read_bytes() == payload
