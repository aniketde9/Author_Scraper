"""Collect discard / stage stats for HTML summary."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path


@dataclass
class RunReport:
    stage_counts: dict[str, int] = field(default_factory=dict)
    discard_reasons: Counter[str] = field(default_factory=Counter)
    extra_notes: list[str] = field(default_factory=list)

    def record_discard(self, stage: str, reason: str, detail: str = "") -> None:
        key = f"{stage}:{reason}"
        if detail:
            key = f"{key}|{detail[:120]}"
        self.discard_reasons[key] += 1

    def set_stage_count(self, stage: str, count: int) -> None:
        self.stage_counts[stage] = count

    def to_html(self, log_path: str | None = None) -> str:
        rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{v}</td></tr>" for k, v in self.discard_reasons.most_common()
        )
        sc = "".join(
            f"<tr><td>{escape(s)}</td><td>{c}</td></tr>" for s, c in sorted(self.stage_counts.items())
        )
        notes = "".join(f"<li>{escape(n)}</li>" for n in self.extra_notes)
        log_line = f"<p>Log file: {escape(log_path or '')}</p>" if log_path else ""
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pipeline summary</title></head>
<body>
<h1>Author Lead Scraper — Run summary</h1>
<p>Generated: {escape(datetime.now().isoformat(timespec="seconds"))}</p>
{log_line}
<h2>Stage output counts</h2>
<table border="1" cellpadding="4"><thead><tr><th>Stage</th><th>Count</th></tr></thead>
<tbody>{sc or "<tr><td colspan='2'>No counts</td></tr>"}</tbody></table>
<h2>Discards (aggregated)</h2>
<table border="1" cellpadding="4"><thead><tr><th>Reason</th><th>Count</th></tr></thead>
<tbody>{rows or "<tr><td colspan='2'>None recorded</td></tr>"}</tbody></table>
<h2>Notes</h2>
<ul>{notes or "<li>—</li>"}</ul>
</body></html>"""

    def write_html(self, project_root: Path, log_path: str | None = None) -> Path:
        out = project_root / "logs" / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        out.write_text(self.to_html(log_path=log_path), encoding="utf-8")
        return out


_REPORT = RunReport()


def get_report() -> RunReport:
    return _REPORT


def reset_report() -> None:
    global _REPORT
    _REPORT = RunReport()
