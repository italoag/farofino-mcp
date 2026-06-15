"""Unit tests for the FaroFino MCP server and analysis engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from farofino_mcp import __main__ as server
from farofino_mcp import engine
from farofino_mcp.schema import Finding, ScanReport, normalize_severity, severity_counts


def _cmd(returncode=0, stdout="", stderr=""):
    return engine.CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_runner(result):
    async def runner(args, timeout=engine.DEFAULT_TIMEOUT, cwd=None, env=None):
        return result

    return runner


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_normalize_severity_aliases():
    assert normalize_severity("High") == "high"
    assert normalize_severity("Informational") == "informational"
    assert normalize_severity("warning") == "low"
    assert normalize_severity(None) == "unknown"
    assert normalize_severity("totally-made-up") == "unknown"


def test_finding_location_and_counts():
    f1 = Finding(tool="slither", title="x", severity="High", file="A.sol", lines=[10, 11, 12])
    f2 = Finding(tool="pattern", title="y", severity="low", file="A.sol", lines=[5])
    assert f1.location == "A.sol:10-12"
    assert f2.location == "A.sol:5"
    counts = severity_counts([f1, f2])
    assert counts["high"] == 1 and counts["low"] == 1 and counts["total"] == 2


def test_scan_report_roundtrip_and_markdown():
    report = ScanReport(
        target="proj",
        framework="foundry",
        tools_run=["slither"],
        findings=[
            Finding(tool="slither", title="reentrancy", severity="high", file="V.sol", lines=[3])
        ],
    )
    rebuilt = ScanReport.from_dict(report.to_dict())
    assert rebuilt.target == "proj"
    assert rebuilt.findings[0].severity == "high"
    md = report.to_markdown()
    assert "reentrancy" in md and "high" in md


# --------------------------------------------------------------------------- #
# Slither
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_slither_success(monkeypatch, tmp_path):
    contract = tmp_path / "Test.sol"
    contract.write_text("pragma solidity ^0.8.0; contract Test {}")

    monkeypatch.setattr(engine, "command_exists", lambda name: name == "slither")
    payload = {"success": True, "error": None, "results": {"detectors": []}}
    monkeypatch.setattr(engine, "_run_command", _fake_runner(_cmd(stdout=json.dumps(payload))))

    result = await engine.run_slither(str(contract))
    assert result.success
    assert result.findings == []


@pytest.mark.asyncio
async def test_run_slither_findings_with_nonzero_exit(monkeypatch, tmp_path):
    """Slither exits non-zero when it finds issues — must still be parsed as success."""
    contract = tmp_path / "Vuln.sol"
    contract.write_text("pragma solidity ^0.8.0; contract Vuln {}")

    monkeypatch.setattr(engine, "command_exists", lambda name: name == "slither")
    payload = {
        "success": True,
        "error": None,
        "results": {
            "detectors": [
                {
                    "check": "reentrancy-eth",
                    "impact": "High",
                    "confidence": "Medium",
                    "description": "Reentrancy in Vuln.withdraw()",
                    "elements": [
                        {
                            "type": "function",
                            "name": "withdraw",
                            "type_specific_fields": {
                                "parent": {"type": "contract", "name": "Vuln"}
                            },
                            "source_mapping": {"filename_relative": "Vuln.sol", "lines": [10, 11]},
                        }
                    ],
                }
            ]
        },
    }
    # returncode 255 mimics Slither flagging issues.
    monkeypatch.setattr(
        engine, "_run_command", _fake_runner(_cmd(returncode=255, stdout=json.dumps(payload)))
    )

    result = await engine.run_slither(str(contract))
    assert result.success
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["severity"] == "high"
    assert finding["detector"] == "reentrancy-eth"
    assert finding["contract"] == "Vuln"
    assert finding["function"] == "withdraw"
    assert finding["file"] == "Vuln.sol"


@pytest.mark.asyncio
async def test_run_slither_compile_failure(monkeypatch, tmp_path):
    contract = tmp_path / "Broken.sol"
    contract.write_text("pragma solidity ^0.8.0; contract {")

    monkeypatch.setattr(engine, "command_exists", lambda name: name == "slither")
    payload = {"success": False, "error": "compilation failed", "results": {}}
    monkeypatch.setattr(
        engine, "_run_command", _fake_runner(_cmd(returncode=1, stdout=json.dumps(payload)))
    )

    result = await engine.run_slither(str(contract))
    assert not result.success
    assert "compilation failed" in (result.error or "")


@pytest.mark.asyncio
async def test_run_slither_missing_file():
    result = await engine.run_slither("/non/existent.sol")
    assert not result.success
    assert "not found" in (result.error or "").lower()


# --------------------------------------------------------------------------- #
# Aderyn
# --------------------------------------------------------------------------- #


def test_normalize_aderyn_findings():
    report = {
        "high_issues": {
            "issues": [
                {
                    "title": "Unprotected initializer",
                    "description": "init can be called by anyone",
                    "detector_name": "unprotected-initializer",
                    "instances": [
                        {"contract_path": "src/A.sol", "line_no": 42, "contract_name": "A"}
                    ],
                }
            ]
        },
        "low_issues": {"issues": []},
    }
    findings = engine.normalize_aderyn_findings(report)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].file == "src/A.sol"
    assert findings[0].lines == [42]


@pytest.mark.asyncio
async def test_run_aderyn_parses_json_output(monkeypatch, tmp_path):
    contract = tmp_path / "A.sol"
    contract.write_text("pragma solidity ^0.8.0; contract A {}")

    monkeypatch.setattr(engine, "command_exists", lambda name: name == "aderyn")

    sample = {
        "high_issues": {
            "issues": [
                {
                    "title": "Dangerous selfdestruct",
                    "description": "",
                    "detector_name": "selfdestruct",
                    "instances": [{"contract_path": "A.sol", "line_no": 3}],
                }
            ]
        },
        "low_issues": {"issues": []},
    }

    async def runner(args, timeout=engine.DEFAULT_TIMEOUT, cwd=None, env=None):
        out = args[args.index("--output") + 1]
        Path(out).write_text(json.dumps(sample))
        return _cmd(returncode=0)

    monkeypatch.setattr(engine, "_run_command", runner)

    result = await engine.run_aderyn(str(contract))
    assert result.success
    assert len(result.findings) == 1
    assert result.findings[0]["detector"] == "selfdestruct"


@pytest.mark.asyncio
async def test_run_aderyn_requires_binary(tmp_path, monkeypatch):
    contract = tmp_path / "Test.sol"
    contract.write_text("pragma solidity ^0.8.0;")
    monkeypatch.setattr(engine, "command_exists", lambda _: False)

    result = await engine.run_aderyn(str(contract))
    assert not result.success
    assert "Aderyn is not installed" in (result.error or "")


# --------------------------------------------------------------------------- #
# Pattern analysis
# --------------------------------------------------------------------------- #


def test_pattern_analysis_detects_real_issue(tmp_path):
    contract = tmp_path / "Risky.sol"
    contract.write_text(
        "// SPDX-License-Identifier: MIT\n"
        "pragma solidity ^0.6.0;\n"
        "contract Risky {\n"
        "    function bad(address target) external {\n"
        '        (bool ok, ) = target.call{value: 1 ether}("");\n'
        "        require(ok);\n"
        "    }\n"
        "}\n"
    )
    result = engine.analyze_contract_patterns(str(contract))
    assert result.success
    detectors = {f["detector"] for f in result.findings}
    assert "low-level-call-with-value" in detectors


def test_pattern_analysis_ignores_comments_and_strings(tmp_path):
    """Keywords inside comments/strings must NOT be flagged."""
    contract = tmp_path / "Clean.sol"
    contract.write_text(
        "// SPDX-License-Identifier: MIT\n"
        "pragma solidity ^0.8.0;\n"
        "// This contract never calls selfdestruct or delegatecall.\n"
        "contract Clean {\n"
        '    string public note = "do not use tx.origin";\n'
        "    uint256 public x;\n"
        "    function set(uint256 v) external { x = v; }\n"
        "}\n"
    )
    result = engine.analyze_contract_patterns(str(contract))
    assert result.success
    # selfdestruct/delegatecall/tx.origin all appear only in comment/string → no findings.
    assert result.findings == []


def test_pattern_analysis_flags_real_selfdestruct(tmp_path):
    contract = tmp_path / "Boom.sol"
    contract.write_text(
        "pragma solidity ^0.8.0;\n"
        "contract Boom {\n"
        "    function kill() external { selfdestruct(payable(msg.sender)); }\n"
        "}\n"
    )
    result = engine.analyze_contract_patterns(str(contract))
    detectors = {f["detector"] for f in result.findings}
    assert "selfdestruct-usage" in detectors


# --------------------------------------------------------------------------- #
# audit_project (combined)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_audit_project_merges_and_reports_skips(monkeypatch, tmp_path):
    contract = tmp_path / "Token.sol"
    contract.write_text(
        "pragma solidity ^0.8.0;\n"
        "contract Token { function kill() external { selfdestruct(payable(msg.sender)); } }\n"
    )

    # Only slither available; aderyn missing.
    monkeypatch.setattr(engine, "command_exists", lambda name: name == "slither")
    payload = {"success": True, "results": {"detectors": []}}
    monkeypatch.setattr(engine, "_run_command", _fake_runner(_cmd(stdout=json.dumps(payload))))

    result = await engine.audit_project(str(contract))
    assert result.success
    report = json.loads(result.output)
    assert "slither" in report["tools_run"]
    assert "pattern" in report["tools_run"]  # file target → pattern runs
    assert report["tools_skipped"].get("aderyn") == "not installed"
    # pattern picked up the real selfdestruct
    assert any(f["detector"] == "selfdestruct-usage" for f in report["findings"])


# --------------------------------------------------------------------------- #
# read_contract / check_tools / dispatch
# --------------------------------------------------------------------------- #


def test_read_contract_returns_source(tmp_path):
    contract = tmp_path / "Read.sol"
    contract.write_text("pragma solidity ^0.8.0;")
    result = engine.read_contract(str(contract))
    assert result.success
    assert "pragma solidity" in (result.output or "")


@pytest.mark.asyncio
async def test_check_tools_reports_availability(monkeypatch):
    states = {"slither": True, "aderyn": False, "solc": True, "solc-select": False, "forge": True}
    monkeypatch.setattr(engine, "command_exists", lambda name: states.get(name, False))

    async def fake_version(cmds):
        return "1.2.3"

    monkeypatch.setattr(engine, "_get_tool_version", fake_version)

    result = await engine.check_tools()
    assert result.success
    payload = json.loads(result.output or "{}")
    assert "slither" in payload["available"]
    assert "aderyn" in payload["missing"]
    assert payload["toolDetails"]["slither"]["version"] == "1.2.3"


@pytest.mark.asyncio
async def test_execute_tool_unknown():
    result = await server.execute_tool("unknown", {})
    assert not result.success
    assert "Unknown tool" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_tool_slither_missing_argument():
    result = await server.execute_tool("slither_audit", {})
    assert not result.success
    assert "Missing required argument" in (result.error or "")


def test_tool_definitions_match_expected():
    tool_names = {tool.name for tool in server.get_tool_definitions()}
    assert tool_names == {
        "audit_project",
        "slither_audit",
        "aderyn_audit",
        "pattern_analysis",
        "read_contract",
        "check_tools",
    }
