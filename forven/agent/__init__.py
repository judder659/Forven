# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forven agent harness — drive the running Forven backend over its HTTP API.

This is the transport-neutral way for ANY AI harness (Claude Code, Codex, a
Tauri sidecar, CI, a plain script) to use Forven without the MCP server. The
MCP server (`forven.mcp_server`) is itself just a thin stdio wrapper over the
same `:8003` REST API this client targets; everything the MCP can do, this can
do, with zero third-party dependencies (stdlib `urllib` only).

Quick start:
    from forven.agent import ForvenAgentClient
    fc = ForvenAgentClient()              # defaults to http://127.0.0.1:8003
    print(fc.health())
    res = fc.run_backtest("S02545", "BTC/USDT-1h")
    print(ForvenAgentClient.metrics(res))

Or from a shell (great for Claude Code / Codex):
    python -m forven.agent health
    python -m forven.agent backtest --strategy S02545 --dataset BTC/USDT-1h --compact
"""

from forven.agent.client import ForvenAgentClient, ForvenAPIError, QUICK_SCREEN_THRESHOLDS

__all__ = ["ForvenAgentClient", "ForvenAPIError", "QUICK_SCREEN_THRESHOLDS"]
