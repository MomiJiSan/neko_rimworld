from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.semantic_tools import SemanticInteraction
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError


@pytest.mark.asyncio
async def test_native_world_change_is_sticky_and_blocks_later_native_calls():
    interaction = object.__new__(SemanticInteraction)
    interaction.session = SimpleNamespace(observations=[])
    interaction.sample_identity = None
    calls = []

    async def handler(args):
        calls.append(args)
        load = "old" if len(calls) == 1 else "new"
        value = {
            "observation_id": str(len(calls)),
            "response": {
                "colony_id": "colony",
                "load_epoch": load,
                "payload": {"status": "ok", "data": {"results": []}, "issues": []},
            },
        }
        return SimpleNamespace(to_dict=lambda: value)

    interaction.original = {"query": SimpleNamespace(handler=handler)}
    await interaction.execute_native({"from": "maps"})
    with pytest.raises(ToolExecutionError) as changed:
        await interaction.execute_native({"from": "maps"})
    assert changed.value.code == "world_changed" and not changed.value.recoverable
    assert interaction.session.invalidated_error == "world_changed"
    assert len(interaction.session.observations) == 1
    with pytest.raises(ToolExecutionError):
        await interaction.execute_native({"from": "maps"})
    assert len(calls) == 2
