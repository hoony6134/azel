import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connectors  # noqa: E402

LISTED = """Checking MCP server health…

claude.ai Cygnus MCP: https://mcp.scian.xyz - ✔ Connected
claude.ai Microsoft 365: https://microsoft365.mcp.claude.com/mcp - ! Needs authentication
claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ✔ Connected
plugin:figma:figma: https://mcp.figma.com/mcp (HTTP) - ! Needs authentication
rea: uvx rea-mcp - ✘ Failed to connect
"""


def test_parse_mcp_list_keeps_only_claude_ai_connectors():
    assert connectors.parse_mcp_list(LISTED) == [
        {"name": "Cygnus MCP", "url": "https://mcp.scian.xyz", "status": "connected"},
        {"name": "Microsoft 365", "url": "https://microsoft365.mcp.claude.com/mcp", "status": "needs_auth"},
        {"name": "Google Calendar", "url": "https://calendarmcp.googleapis.com/mcp/v1", "status": "connected"}]


def test_secret_file_must_be_private(tmp_path):
    connectors.put_secret(tmp_path, "wiki", '{"X-Api-Key": "k"}')
    assert connectors._headers(tmp_path, "wiki") == {"X-Api-Key": "k"}
    (tmp_path / "mcp" / "wiki").chmod(0o644)
    assert connectors._headers(tmp_path, "wiki") is None  # readable by others: skipped, never sent
    assert connectors._headers(tmp_path, "missing") is None


def test_no_discovery_yet_loads_every_connector_but_no_plugins(tmp_path):
    d = connectors.Discovery()
    d.plugins = ["p@m"]
    servers, allowed, settings, env = connectors.run_args({}, d, [], "t", tmp_path)
    assert '"allowedMcpServers"' not in settings and '"p@m": false' in settings and env == {"ENABLE_TOOL_SEARCH": "false"}
