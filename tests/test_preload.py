"""最初から載せる道具（PETIT_PRELOAD_TOOLS）の印。一覧は petit-env の scripts/preload-tools.txt（2026-10-03）。"""

from mcp import types
from mcp.types import Tool

from memory_mcp import preload
from memory_mcp.server import MemoryMCPServer

ALWAYS = "anthropic/alwaysLoad"


async def _listed(monkeypatch, value: str | None) -> dict[str, dict]:
    if value is None:
        monkeypatch.delenv("PETIT_PRELOAD_TOOLS", raising=False)
    else:
        monkeypatch.setenv("PETIT_PRELOAD_TOOLS", value)
    server = MemoryMCPServer()
    handler = server._server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest(method="tools/list"))
    # claude に届く形（_meta の名前で JSON にしたもの）で見る
    dumped = result.root.model_dump(by_alias=True, exclude_none=True)
    return {t["name"]: t.get("_meta") or {} for t in dumped["tools"]}


async def test_listed_tools_get_always_load(monkeypatch):
    tools = await _listed(monkeypatch, "remember, save_visual_memory")
    assert {n for n, m in tools.items() if m.get(ALWAYS)} == {"remember", "save_visual_memory"}


async def test_nothing_marked_without_the_env(monkeypatch):
    for value in (None, ""):
        tools = await _listed(monkeypatch, value)
        assert len(tools) > 10 and not any(m.get(ALWAYS) for m in tools.values())


def test_unknown_name_is_reported_once(capsys):
    preload._warned.clear()
    tools = [Tool(name="remember", inputSchema={"type": "object"})]
    preload.mark_preload(tools, "remember,no_such_tool")
    assert tools[0].meta == {ALWAYS: True}
    assert "no_such_tool" in capsys.readouterr().err
    preload.mark_preload([Tool(name="remember", inputSchema={"type": "object"})], "remember,no_such_tool")
    assert capsys.readouterr().err == ""
