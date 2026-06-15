#!/usr/bin/env python3
"""MCP Smart Contract Auditor Server.

A Model Context Protocol (MCP) server for auditing smart contracts using
Slither, Aderyn, and heuristic pattern analysis. The actual analysis logic
lives in :mod:`farofino_mcp.engine`; this module only wires it to MCP.
"""

import asyncio
import json
import sys
from typing import Any, Dict, List

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import engine
from .engine import (  # re-exported for convenience / backwards compatibility
    AuditResult,
    analyze_contract_patterns,
    audit_project,
    check_tools,
    read_contract,
    run_aderyn,
    run_slither,
)

__all__ = [
    "AuditResult",
    "analyze_contract_patterns",
    "audit_project",
    "check_tools",
    "read_contract",
    "run_aderyn",
    "run_slither",
    "get_tool_definitions",
    "execute_tool",
    "main",
    "run",
]

_CONTRACT_PATH_PROP = {
    "contract_path": {
        "type": "string",
        "description": "Path to a smart contract file (.sol/.vy) or a project root directory",
    }
}


def get_tool_definitions() -> List[Tool]:
    """Return the MCP tool definitions exposed by this server."""
    return [
        Tool(
            name="audit_project",
            description=(
                "Run all available static analyzers (Slither, Aderyn, heuristic patterns) on a "
                "contract file or project directory and return a single normalized findings report. "
                "This is the recommended entry point for a full static-analysis pass."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_CONTRACT_PATH_PROP,
                    "tools": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["slither", "aderyn", "pattern"]},
                        "description": "Optional subset of analyzers to run (default: all available)",
                    },
                    "detectors": {
                        "type": "string",
                        "description": "Optional: comma-separated Slither detectors to run",
                    },
                    "exclude_detectors": {
                        "type": "string",
                        "description": "Optional: comma-separated Slither detectors to exclude",
                    },
                    "solc_version": {
                        "type": "string",
                        "description": "Optional: solc version to select via solc-select (e.g. 0.8.20)",
                    },
                },
                "required": ["contract_path"],
            },
        ),
        Tool(
            name="slither_audit",
            description=(
                "Run Slither static analysis on a smart contract file or project directory. "
                "Returns normalized findings. Slither is a Solidity & Vyper static analysis framework."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_CONTRACT_PATH_PROP,
                    "detectors": {
                        "type": "string",
                        "description": "Optional: comma-separated list of specific detectors to run",
                    },
                    "exclude_detectors": {
                        "type": "string",
                        "description": "Optional: comma-separated list of detectors to exclude",
                    },
                    "solc_version": {
                        "type": "string",
                        "description": "Optional: solc version to select via solc-select (e.g. 0.8.20)",
                    },
                },
                "required": ["contract_path"],
            },
        ),
        Tool(
            name="aderyn_audit",
            description=(
                "Run Aderyn static analysis on a smart contract file or project root. Aderyn is a "
                "Rust-based Solidity analyzer; results are returned as normalized findings."
            ),
            inputSchema={
                "type": "object",
                "properties": {**_CONTRACT_PATH_PROP},
                "required": ["contract_path"],
            },
        ),
        Tool(
            name="pattern_analysis",
            description=(
                "Run lightweight, comment-aware heuristic checks (reentrancy hints, tx.origin, "
                "selfdestruct, delegatecall, etc.) on a single contract file. Low-confidence by design."
            ),
            inputSchema={
                "type": "object",
                "properties": {**_CONTRACT_PATH_PROP},
                "required": ["contract_path"],
            },
        ),
        Tool(
            name="read_contract",
            description="Read and return the source code of a smart contract file.",
            inputSchema={
                "type": "object",
                "properties": {**_CONTRACT_PATH_PROP},
                "required": ["contract_path"],
            },
        ),
        Tool(
            name="check_tools",
            description=(
                "Check which audit tools (slither, aderyn, solc, solc-select, forge) are installed, "
                "with versions and installation instructions."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


async def execute_tool(name: str, arguments: Dict[str, Any]) -> AuditResult:
    """Dispatch execution to the correct engine handler."""
    contract_path = arguments.get("contract_path")

    if name == "audit_project":
        return await engine.audit_project(
            contract_path,
            tools=arguments.get("tools"),
            detectors=arguments.get("detectors"),
            exclude_detectors=arguments.get("exclude_detectors"),
            solc_version=arguments.get("solc_version"),
        )
    if name == "slither_audit":
        return await engine.run_slither(
            contract_path,
            arguments.get("detectors"),
            arguments.get("exclude_detectors"),
            arguments.get("solc_version"),
        )
    if name == "aderyn_audit":
        return await engine.run_aderyn(contract_path)
    if name == "pattern_analysis":
        return engine.analyze_contract_patterns(contract_path)
    if name == "read_contract":
        return engine.read_contract(contract_path)
    if name == "check_tools":
        return await engine.check_tools()

    return AuditResult(success=False, error=f"Unknown tool: {name}")


async def main() -> None:
    """Main entry point for the MCP server."""
    server: Server = Server("farofino-mcp")
    tools = get_tool_definitions()

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> List[TextContent]:
        try:
            result = await execute_tool(name, arguments)
            if not result.success:
                return [TextContent(type="text", text=result.error or "Unknown error occurred")]
            payload = result.output
            if payload is None and result.findings:
                payload = json.dumps(result.findings, indent=2)
            return [TextContent(type="text", text=payload or "")]
        except Exception as exc:  # pragma: no cover - defensive guard
            return [TextContent(type="text", text=f"Error executing tool: {exc}")]

    async with stdio_server() as (read_stream, write_stream):
        print("MCP Smart Contract Auditor Server running on stdio", file=sys.stderr)
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    """Entry point for the console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
