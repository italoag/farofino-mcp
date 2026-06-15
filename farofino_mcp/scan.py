#!/usr/bin/env python3
"""Command-line static-analysis scanner — the FaroFino skills' fallback engine.

Runs the same analysis engine as the MCP server, but as a plain CLI so the
FaroFino skills can call it directly when the MCP server is not registered:

    python -m farofino_mcp.scan <path> [--format json|markdown] [--output FILE]

Exit codes:
    0  scan completed (with or without findings)
    2  usage error / target missing / no analyzers available
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List, Optional

from . import engine
from .schema import ScanReport


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="farofino-mcp-scan",
        description="Run Slither/Aderyn/pattern static analysis and emit a normalized report.",
    )
    parser.add_argument("target", help="Contract file (.sol/.vy) or project root directory")
    parser.add_argument(
        "--tools",
        help="Comma-separated subset of analyzers to run (slither,aderyn,pattern)",
    )
    parser.add_argument("--detectors", help="Comma-separated Slither detectors to run")
    parser.add_argument(
        "--exclude", dest="exclude", help="Comma-separated Slither detectors to exclude"
    )
    parser.add_argument("--solc-version", dest="solc_version", help="solc version for solc-select")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument("--output", help="Write the report to this file instead of stdout")
    return parser


async def _run(args: argparse.Namespace) -> int:
    tools: Optional[List[str]] = (
        [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None
    )
    result = await engine.audit_project(
        args.target,
        tools=tools,
        detectors=args.detectors,
        exclude_detectors=args.exclude,
        solc_version=args.solc_version,
    )

    if not result.success:
        print(result.error or "scan failed", file=sys.stderr)
        return 2

    report_dict = json.loads(result.output or "{}")
    if args.format == "markdown":
        rendered = ScanReport.from_dict(report_dict).to_markdown()
    else:
        rendered = json.dumps(report_dict, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(rendered)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
