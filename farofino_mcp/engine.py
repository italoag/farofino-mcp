"""Analysis engine for the FaroFino MCP auditor.

This module contains the actual tool execution and output-normalization logic.
It is intentionally decoupled from the MCP server (`__main__.py`) and the CLI
(`cli.py`) so that the same engine backs every entry point:

* the MCP tools (host-mediated),
* the ``farofino-mcp scan`` CLI used as the skills' fallback,
* and the unit tests.

All command execution goes through :func:`_run_command` (async) so callers never
block the event loop and tests can monkeypatch a single seam.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .schema import Finding, ScanReport, normalize_severity

DEFAULT_TIMEOUT = 300

TOOL_INSTALL_INSTRUCTIONS: Dict[str, str] = {
    "slither": "pip install slither-analyzer",
    "aderyn": (
        "curl -LsSf https://raw.githubusercontent.com/Cyfrin/up/main/install | bash "
        "&& CYFRINUP_ONLY_INSTALL=aderyn cyfrinup"
    ),
    "solc": "pip install solc-select && solc-select install <version>",
    "solc-select": "pip install solc-select",
    "forge": ("curl -L https://foundry.paradigm.xyz | bash && foundryup"),
}

TOOL_VERSION_COMMANDS: Dict[str, List[List[str]]] = {
    "slither": [["slither", "--version"]],
    "aderyn": [["aderyn", "--version"]],
    "solc": [["solc", "--version"]],
    "solc-select": [["solc-select", "--version"]],
    "forge": [["forge", "--version"]],
}


@dataclass
class CommandResult:
    """Lightweight stand-in for ``subprocess.CompletedProcess``."""

    returncode: int
    stdout: str
    stderr: str


@dataclass
class AuditResult:
    """Result of a single analysis operation."""

    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    findings: List[Any] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Filesystem / process helpers
# --------------------------------------------------------------------------- #


def command_exists(command: str) -> bool:
    """Check if a command exists on the system PATH."""
    return shutil.which(command) is not None


def file_exists(path: Optional[str]) -> bool:
    """Check if a path points to an existing file."""
    if not path:
        return False
    return Path(path).is_file()


def path_exists(path: Optional[str]) -> bool:
    """Check if a path points to an existing file *or* directory."""
    if not path:
        return False
    return Path(path).exists()


async def _run_command(
    args: Sequence[str],
    timeout: int = DEFAULT_TIMEOUT,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> CommandResult:
    """Run a command asynchronously and capture stdout/stderr.

    Raises ``FileNotFoundError`` if the binary is missing and
    ``asyncio.TimeoutError`` if the command exceeds ``timeout`` seconds.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return CommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
        stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
    )


# --------------------------------------------------------------------------- #
# Project detection / solc handling
# --------------------------------------------------------------------------- #


def detect_framework(target: str) -> str:
    """Best-effort detection of the project framework for ``target``."""
    root = Path(target)
    if root.is_file():
        root = root.parent
    # Walk up a couple of levels looking for a project manifest.
    for candidate in [root, *root.parents][:4]:
        if (candidate / "foundry.toml").is_file():
            return "foundry"
        if any(
            (candidate / name).is_file()
            for name in ("hardhat.config.js", "hardhat.config.ts", "hardhat.config.cjs")
        ):
            return "hardhat"
    return "unknown"


_PRAGMA_RE = re.compile(r"pragma\s+solidity\s+[^;]*?(\d+\.\d+\.\d+)")


def detect_pragma_version(target: str) -> Optional[str]:
    """Read the first concrete ``pragma solidity`` version from a .sol file."""
    if not file_exists(target):
        return None
    try:
        content = Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _PRAGMA_RE.search(content)
    return match.group(1) if match else None


async def _maybe_select_solc(version: Optional[str]) -> Optional[str]:
    """Best-effort ``solc-select`` activation. Never raises."""
    if not version or not command_exists("solc-select"):
        return None
    try:
        await _run_command(["solc-select", "install", version], timeout=120)
        await _run_command(["solc-select", "use", version], timeout=60)
        return version
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Slither
# --------------------------------------------------------------------------- #


def _extract_slither_location(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull contract / function / file / lines from a Slither detector's elements."""
    info: Dict[str, Any] = {"contract": None, "function": None, "file": None, "lines": []}
    for element in elements:
        etype = element.get("type")
        mapping = element.get("source_mapping") or {}
        if not info["file"] and mapping.get("filename_relative"):
            info["file"] = mapping.get("filename_relative")
            info["lines"] = mapping.get("lines") or []
        if etype == "function" and not info["function"]:
            info["function"] = element.get("name")
            parent = (element.get("type_specific_fields") or {}).get("parent") or {}
            if parent.get("type") == "contract":
                info["contract"] = parent.get("name")
        elif etype == "contract" and not info["contract"]:
            info["contract"] = element.get("name")
    return info


def parse_slither_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse Slither's ``--json -`` payload. Returns ``None`` when not JSON."""
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Slither sometimes prefixes logs; try to recover the JSON object.
        start = stdout.find("{")
        if start == -1:
            return None
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            return None


def normalize_slither_findings(detectors: List[Dict[str, Any]]) -> List[Finding]:
    findings: List[Finding] = []
    for det in detectors:
        loc = _extract_slither_location(det.get("elements") or [])
        description = (det.get("description") or "").strip()
        title = description.splitlines()[0] if description else det.get("check", "slither")
        findings.append(
            Finding(
                tool="slither",
                title=title[:160],
                severity=normalize_severity(det.get("impact")),
                description=description,
                detector=det.get("check"),
                confidence=det.get("confidence"),
                contract=loc["contract"],
                function=loc["function"],
                file=loc["file"],
                lines=loc["lines"],
            )
        )
    return findings


async def run_slither(
    target: Optional[str],
    detectors: Optional[str] = None,
    exclude_detectors: Optional[str] = None,
    solc_version: Optional[str] = None,
) -> AuditResult:
    """Run Slither on a single file or a whole project directory."""
    if not target:
        return AuditResult(success=False, error="Missing required argument: contract_path")
    if not path_exists(target):
        return AuditResult(success=False, error=f"Target not found: {target}")
    if not command_exists("slither"):
        return AuditResult(
            success=False,
            error="Slither is not installed. Install it with: pip install slither-analyzer",
        )

    # Try to align the solc version with the contract pragma (best-effort).
    await _maybe_select_solc(solc_version or detect_pragma_version(target))

    is_dir = Path(target).is_dir()
    # For a project root, run from inside it (as ".") so remappings/config resolve.
    cwd = target if is_dir else None
    scan_target = "." if is_dir else target

    args = ["slither", scan_target, "--json", "-"]
    if detectors:
        args.extend(["--detect", detectors])
    if exclude_detectors:
        args.extend(["--exclude", exclude_detectors])

    try:
        result = await _run_command(args, timeout=DEFAULT_TIMEOUT, cwd=cwd)
    except asyncio.TimeoutError:
        return AuditResult(success=False, error="Slither analysis timed out")
    except FileNotFoundError:
        return AuditResult(success=False, error="Slither is not installed.")
    except OSError as exc:
        return AuditResult(success=False, error=f"Slither analysis failed: {exc}")

    parsed = parse_slither_json(result.stdout)
    if parsed is not None:
        # Slither produced JSON: trust it regardless of the process exit code.
        if parsed.get("success") is False:
            message = parsed.get("error") or result.stderr.strip() or "Slither reported failure"
            return AuditResult(success=False, error=f"Slither analysis failed: {message}")
        detectors_out = (parsed.get("results") or {}).get("detectors") or []
        findings = normalize_slither_findings(detectors_out)
        return AuditResult(
            success=True,
            output=result.stdout,
            findings=[f.to_dict() for f in findings],
        )

    # No JSON parsed → only a non-zero exit code is a real failure.
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return AuditResult(success=False, error=f"Slither analysis failed: {message}")
    return AuditResult(success=True, output=result.stdout, findings=[])


# --------------------------------------------------------------------------- #
# Aderyn
# --------------------------------------------------------------------------- #


def normalize_aderyn_findings(report: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    for bucket, severity in (
        ("high_issues", "high"),
        ("low_issues", "low"),
        ("medium_issues", "medium"),
    ):
        issues = (report.get(bucket) or {}).get("issues") or []
        for issue in issues:
            instances = issue.get("instances") or []
            first = instances[0] if instances else {}
            line_no = first.get("line_no")
            findings.append(
                Finding(
                    tool="aderyn",
                    title=(issue.get("title") or "aderyn issue")[:160],
                    severity=severity,
                    description=(issue.get("description") or "").strip(),
                    detector=issue.get("detector_name"),
                    contract=first.get("contract_name"),
                    file=first.get("contract_path"),
                    lines=[line_no] if isinstance(line_no, int) else [],
                )
            )
    return findings


async def run_aderyn(target: Optional[str]) -> AuditResult:
    """Run Aderyn, preferring structured JSON output."""
    if not path_exists(target):
        return AuditResult(success=False, error=f"Target not found: {target}")
    if not command_exists("aderyn"):
        return AuditResult(
            success=False,
            error=(
                "Aderyn is not installed. Install it with Cyfrinup: "
                "curl -LsSf https://raw.githubusercontent.com/Cyfrin/up/main/install | bash "
                "&& CYFRINUP_ONLY_INSTALL=aderyn cyfrinup"
            ),
        )

    assert target is not None
    root = target if Path(target).is_dir() else str(Path(target).parent)
    out_path = os.path.join(tempfile.mkdtemp(prefix="aderyn-"), "report.json")

    try:
        result = await _run_command(
            ["aderyn", target, "--output", out_path], timeout=DEFAULT_TIMEOUT, cwd=root
        )
    except asyncio.TimeoutError:
        return AuditResult(success=False, error="Aderyn analysis timed out")
    except FileNotFoundError:
        return AuditResult(success=False, error="Aderyn is not installed.")
    except OSError as exc:
        return AuditResult(success=False, error=f"Aderyn analysis failed: {exc}")

    # Aderyn may write the report relative to cwd; check both candidate paths.
    candidates = [out_path, os.path.join(root, "report.json")]
    report_path = next((p for p in candidates if os.path.isfile(p)), None)

    if report_path:
        try:
            data = json.loads(Path(report_path).read_text(encoding="utf-8", errors="replace"))
            findings = normalize_aderyn_findings(data)
            return AuditResult(
                success=True,
                output=json.dumps(data, indent=2),
                findings=[f.to_dict() for f in findings],
            )
        except (json.JSONDecodeError, OSError):
            pass  # fall back to text output below

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return AuditResult(success=False, error=f"Aderyn analysis failed: {message}")
    # Ran but no parseable JSON: return raw text, no structured findings.
    return AuditResult(success=True, output=result.stdout, findings=[])


# --------------------------------------------------------------------------- #
# Heuristic pattern analysis
# --------------------------------------------------------------------------- #


def strip_comments_and_strings(src: str) -> str:
    """Remove comments and string/char literals so keyword checks avoid them.

    This is a heuristic stripper (not a real lexer) — good enough to stop
    matching keywords inside comments, SPDX headers, and string literals.
    """
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)  # block comments
    src = re.sub(r'"(?:\\.|[^"\\])*"', '""', src)  # double-quoted strings
    src = re.sub(r"'(?:\\.|[^'\\])*'", "''", src)  # single-quoted strings
    src = re.sub(r"//[^\n]*", " ", src)  # line comments
    return src


def _pragma_is_08_plus(code: str) -> bool:
    match = _PRAGMA_RE.search(code)
    if not match:
        return False
    try:
        major, minor, _ = (int(p) for p in match.group(1).split("."))
    except (ValueError, IndexError):
        return False
    return major == 0 and minor >= 8


def analyze_contract_patterns(target: Optional[str]) -> AuditResult:
    """Lightweight, comment-aware heuristic checks. Low confidence by design."""
    if not file_exists(target):
        return AuditResult(success=False, error=f"Contract file not found: {target}")

    assert target is not None
    try:
        raw = Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return AuditResult(success=False, error=f"Pattern analysis failed: {exc}")

    code = strip_comments_and_strings(raw)
    rel = os.path.basename(target)
    findings: List[Finding] = []

    def add(title: str, severity: str, description: str) -> None:
        findings.append(
            Finding(
                tool="pattern",
                title=title,
                severity=severity,
                description=description,
                detector=title.lower().replace(" ", "-"),
                confidence="heuristic",
                file=rel,
            )
        )

    if re.search(r"\bselfdestruct\b", code):
        add(
            "selfdestruct usage",
            "medium",
            "Contract contains selfdestruct — potential security risk.",
        )
    if re.search(r"\bdelegatecall\b", code):
        add(
            "delegatecall usage",
            "medium",
            "Contract uses delegatecall — ensure strict access control.",
        )
    if re.search(r"\btx\.origin\b", code):
        add(
            "tx.origin authentication",
            "medium",
            "tx.origin is vulnerable to phishing — use msg.sender.",
        )
    if re.search(r"\bblock\.(timestamp|number)\b", code):
        add(
            "block timing dependency",
            "informational",
            "Uses block.timestamp/number — beware miner influence.",
        )
    if re.search(r"\.call\s*\{\s*value", code):
        add(
            "low-level call with value",
            "low",
            "Potential reentrancy — apply checks-effects-interactions.",
        )
    if not _pragma_is_08_plus(code) and "SafeMath" not in code and re.search(r"[+\-*/%]", code):
        add(
            "missing overflow protection",
            "low",
            "Pre-0.8 arithmetic without SafeMath — consider upgrading or using SafeMath.",
        )

    if findings:
        summary = f"Pattern analysis: {len(findings)} potential issue(s) found."
    else:
        summary = "Pattern analysis: no heuristic issues found."

    return AuditResult(
        success=True,
        output=summary,
        findings=[f.to_dict() for f in findings],
    )


# --------------------------------------------------------------------------- #
# Combined project audit
# --------------------------------------------------------------------------- #


async def audit_project(
    target: Optional[str],
    tools: Optional[Sequence[str]] = None,
    detectors: Optional[str] = None,
    exclude_detectors: Optional[str] = None,
    solc_version: Optional[str] = None,
) -> AuditResult:
    """Run all (or the requested subset of) analyzers and merge their findings."""
    if not path_exists(target):
        return AuditResult(success=False, error=f"Target not found: {target}")

    assert target is not None
    requested = [t.lower() for t in (tools or ("slither", "aderyn", "pattern"))]
    report = ScanReport(target=target, framework=detect_framework(target))
    is_file = Path(target).is_file()

    if "slither" in requested:
        if command_exists("slither"):
            res = await run_slither(target, detectors, exclude_detectors, solc_version)
            if res.success:
                report.tools_run.append("slither")
                report.findings.extend(Finding(**f) for f in res.findings)
            else:
                report.errors["slither"] = res.error or "unknown error"
        else:
            report.tools_skipped["slither"] = "not installed"

    if "aderyn" in requested:
        if command_exists("aderyn"):
            res = await run_aderyn(target)
            if res.success:
                report.tools_run.append("aderyn")
                report.findings.extend(Finding(**f) for f in res.findings)
            else:
                report.errors["aderyn"] = res.error or "unknown error"
        else:
            report.tools_skipped["aderyn"] = "not installed"

    if "pattern" in requested:
        if is_file:
            res = analyze_contract_patterns(target)
            if res.success:
                report.tools_run.append("pattern")
                report.findings.extend(Finding(**f) for f in res.findings)
            else:
                report.errors["pattern"] = res.error or "unknown error"
        else:
            report.tools_skipped["pattern"] = "directory target (use slither/aderyn)"

    return AuditResult(
        success=True,
        output=json.dumps(report.to_dict(), indent=2),
        findings=[f.to_dict() for f in report.findings],
    )


# --------------------------------------------------------------------------- #
# Misc tools
# --------------------------------------------------------------------------- #


def read_contract(target: Optional[str]) -> AuditResult:
    """Read and return a contract's source code."""
    if not file_exists(target):
        return AuditResult(success=False, error=f"Contract file not found: {target}")
    assert target is not None
    try:
        return AuditResult(success=True, output=Path(target).read_text(encoding="utf-8"))
    except OSError as exc:
        return AuditResult(success=False, error=f"Failed to read contract: {exc}")


async def _get_tool_version(commands: List[List[str]]) -> Optional[str]:
    """Attempt to retrieve a tool version using the provided commands."""
    for command in commands:
        try:
            result = await _run_command(command, timeout=30)
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            continue
        if result.returncode != 0:
            continue
        candidate = (result.stdout or result.stderr or "").strip()
        if candidate:
            return candidate.splitlines()[0]
    return None


async def check_tools() -> AuditResult:
    """Report which audit tools (and supporting toolchain) are available."""
    tool_status: Dict[str, Dict[str, Optional[str]]] = {}
    for tool in ("slither", "aderyn", "solc", "solc-select", "forge"):
        installed = command_exists(tool)
        version = (
            await _get_tool_version(TOOL_VERSION_COMMANDS.get(tool, [])) if installed else None
        )
        tool_status[tool] = {"installed": installed, "version": version}

    available = [name for name, status in tool_status.items() if status["installed"]]
    missing = [name for name in tool_status if name not in available]

    payload = {
        "available": available,
        "missing": missing,
        "toolDetails": tool_status,
        "installInstructions": TOOL_INSTALL_INSTRUCTIONS,
    }
    return AuditResult(success=True, output=json.dumps(payload, indent=2), findings=[tool_status])
