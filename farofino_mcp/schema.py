"""Normalized finding schema shared across analyzers (Slither, Aderyn, patterns).

Every analyzer maps its native output into :class:`Finding` objects so that the
MCP tools, the CLI fallback, and the FaroFino skills all consume the *same*
structure regardless of which engine produced it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# Canonical severity ladder, ordered most → least severe.
SEVERITY_ORDER: List[str] = [
    "high",
    "medium",
    "low",
    "informational",
    "optimization",
    "unknown",
]

# Native severity vocabularies → canonical ladder.
_SEVERITY_ALIASES: Dict[str, str] = {
    "high": "high",
    "critical": "high",
    "medium": "medium",
    "med": "medium",
    "low": "low",
    "informational": "informational",
    "info": "informational",
    "note": "informational",
    "optimization": "optimization",
    "gas": "optimization",
    "warning": "low",
}


def normalize_severity(value: Optional[str]) -> str:
    """Map an analyzer-specific severity string onto the canonical ladder."""
    if not value:
        return "unknown"
    return _SEVERITY_ALIASES.get(value.strip().lower(), "unknown")


@dataclass
class Finding:
    """A single normalized security finding."""

    tool: str  # "slither" | "aderyn" | "pattern"
    title: str
    severity: str = "unknown"
    description: str = ""
    detector: Optional[str] = None
    confidence: Optional[str] = None
    contract: Optional[str] = None
    function: Optional[str] = None
    file: Optional[str] = None
    lines: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.severity = normalize_severity(self.severity)

    @property
    def location(self) -> str:
        """Human-readable ``file:line`` (or ``file:start-end``) location."""
        if not self.file:
            return ""
        if not self.lines:
            return self.file
        if len(self.lines) == 1:
            return f"{self.file}:{self.lines[0]}"
        return f"{self.file}:{self.lines[0]}-{self.lines[-1]}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def severity_counts(findings: List[Finding]) -> Dict[str, int]:
    """Return per-severity counts plus a ``total`` for a list of findings."""
    counts: Dict[str, int] = {sev: 0 for sev in SEVERITY_ORDER}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    counts["total"] = len(findings)
    return counts


def sort_findings(findings: List[Finding]) -> List[Finding]:
    """Sort findings by severity (high → unknown), then tool, then title."""
    rank = {sev: idx for idx, sev in enumerate(SEVERITY_ORDER)}
    return sorted(
        findings,
        key=lambda f: (rank.get(f.severity, len(SEVERITY_ORDER)), f.tool, f.title),
    )


@dataclass
class ScanReport:
    """Aggregated result of running one or more analyzers on a target."""

    target: str
    framework: str = "unknown"
    tools_run: List[str] = field(default_factory=list)
    tools_skipped: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScanReport":
        """Rebuild a report from its :meth:`to_dict` form (summary is recomputed)."""
        return cls(
            target=data.get("target", ""),
            framework=data.get("framework", "unknown"),
            tools_run=list(data.get("tools_run") or []),
            tools_skipped=dict(data.get("tools_skipped") or {}),
            errors=dict(data.get("errors") or {}),
            findings=[Finding(**f) for f in (data.get("findings") or [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        ordered = sort_findings(self.findings)
        return {
            "target": self.target,
            "framework": self.framework,
            "tools_run": self.tools_run,
            "tools_skipped": self.tools_skipped,
            "errors": self.errors,
            "summary": severity_counts(ordered),
            "findings": [f.to_dict() for f in ordered],
        }

    def to_markdown(self) -> str:
        ordered = sort_findings(self.findings)
        counts = severity_counts(ordered)
        lines: List[str] = []
        lines.append(f"# Static Analysis — {self.target}")
        lines.append("")
        lines.append(f"- Framework: `{self.framework}`")
        lines.append(f"- Tools run: {', '.join(self.tools_run) or 'none'}")
        if self.tools_skipped:
            skipped = ", ".join(f"{t} ({why})" for t, why in self.tools_skipped.items())
            lines.append(f"- Tools skipped: {skipped}")
        if self.errors:
            errs = ", ".join(f"{t}: {msg}" for t, msg in self.errors.items())
            lines.append(f"- Errors: {errs}")
        summary = " · ".join(f"{sev}: {counts[sev]}" for sev in SEVERITY_ORDER if counts.get(sev))
        lines.append(f"- Findings: {counts['total']} ({summary or 'none'})")
        lines.append("")
        if not ordered:
            lines.append("_No findings reported by the static analyzers._")
            return "\n".join(lines)
        lines.append("| Severity | Tool | Detector | Location | Title |")
        lines.append("|----------|------|----------|----------|-------|")
        for f in ordered:
            lines.append(
                f"| {f.severity} | {f.tool} | {f.detector or '-'} "
                f"| {f.location or '-'} | {f.title} |"
            )
        return "\n".join(lines)
