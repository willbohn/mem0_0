import json
from pathlib import Path

ARTIFACTS = Path(__file__).parents[3] / "deployment" / "mem0_0_arch" / "harnesses" / "omp"
EXPECTED_TOOLS = ("add_memory", "search_memories", "get_memories", "get_memory", "update_memory", "delete_memory")


def test_mcp_example_uses_key_file_and_no_inline_credentials():
    data = json.loads((ARTIFACTS / "mcp.json.example").read_text())
    (server,) = [srv for block in data.values() if isinstance(block, dict) for srv in block.values()]
    env = server["env"]
    assert "MEM0_API_KEY" not in env
    assert env["MEM0_API_KEY_FILE"].endswith("bridge-api-key")
    assert env["MEM0_API_URL"] == "http://127.0.0.1:28889"
    assert Path(server["args"][-1]).name == "bridge.py"


def test_policy_fragment_pins_scope_constants():
    text = (ARTIFACTS / "policy-fragment.md").read_text()
    for required in ("agent_id=omp", "machine_id=arch-local", "project_id=mem0_0", "infer=false", "MEM0_API_KEY_FILE"):
        assert required in text


def test_runbook_lists_exact_tools_and_pending_label():
    text = (ARTIFACTS / "runbook.md").read_text()
    for tool in EXPECTED_TOOLS:
        assert tool in text
    assert "pending installation" in text
