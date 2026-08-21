"""Format ResearchReport for different output modes."""

from __future__ import annotations

from .models import ResearchReport

__all__ = ["format_report"]


def format_report(report: ResearchReport, format: str = "json") -> str:
    """Format a ResearchReport as JSON or Markdown.

    Args:
        report: The research report to format
        format: Output format - "json" or "markdown"

    Returns:
        Formatted report as string
    """
    if format == "markdown":
        return _format_markdown(report)
    elif format == "json":
        return report.model_dump_json(indent=2)
    else:
        raise ValueError(f"Unknown format: {format!r}. Use 'json' or 'markdown'.")


def _format_markdown(report: ResearchReport) -> str:
    """Format report as readable Markdown."""
    lines = []

    # Title and answer
    lines.append("# Research Answer\n")
    lines.append(report.answer.answer)
    lines.append("")

    # Claims with evidence
    if report.answer.claims:
        lines.append("## Claims\n")
        for i, claim in enumerate(report.answer.claims, 1):
            lines.append(f"{i}. {claim.claim_text}")
        lines.append("")

    # Citations
    if report.answer.citations:
        lines.append("## Citations\n")
        for citation in report.answer.citations:
            lines.append(f"**{citation.evidence_id}** — {citation.source_url}")
            lines.append(f"> {citation.snippet}\n")
        lines.append("")

    # Limitations
    if report.answer.limitations:
        lines.append("## Limitations\n")
        for limitation in report.answer.limitations:
            lines.append(f"- {limitation}")
        lines.append("")

    # Retrieval telemetry
    if report.validation_summary:
        lines.append("## Retrieval Summary\n")
        summary = report.validation_summary
        lines.append(f"- **Raw records retrieved**: {summary.get('raw_count', 0)}")
        lines.append(f"- **Records kept**: {summary.get('kept_count', 0)}")
        lines.append(f"- **Dropped (failed)**: {summary.get('dropped_failed', 0)}")
        lines.append(f"- **Dropped (drift)**: {summary.get('dropped_drift', 0)}")
        lines.append(f"- **Dropped (duplicate)**: {summary.get('dropped_duplicate', 0)}")
        lines.append("")

    # Source attempts (if present)
    if report.source_attempts:
        lines.append("## Retrieval Attempts (Telemetry)\n")
        for attempt in report.source_attempts:
            attempt_dict = attempt.model_dump() if hasattr(attempt, 'model_dump') else attempt
            lines.append(
                f"- **{attempt_dict.get('provider', 'unknown')}** "
                f"({attempt_dict.get('action', '?')}): "
                f"{attempt_dict.get('status', '?')} "
                f"({attempt_dict.get('duration_ms', 0):.0f}ms, "
                f"{attempt_dict.get('char_count', 0)} chars)"
            )
        lines.append("")

    return "\n".join(lines)
