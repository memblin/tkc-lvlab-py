"""Reporting — project the registry + results into JSON, text, and issue markdown.

Three renderings of the *same* data:

* :func:`to_json` — machine-readable, the durable run artifact.
* :func:`human_summary` — a scannable terminal recap.
* :func:`issue_markdown` — a GitHub issue body plus per-failure sub-issue
  stubs, so filing the tracker is a copy/paste (never auto-filed).
"""

from __future__ import annotations

import json
from collections import Counter

from validate.model import ScenarioResult, Status


def _result_to_dict(result: ScenarioResult) -> dict:
    """Serialize one :class:`ScenarioResult` to a JSON-safe dict."""
    return {
        "name": result.name,
        "needs": result.needs,
        "tags": result.tags,
        "status": result.status.value,
        "error": result.error,
        "duration_s": round(result.duration_s, 2),
        "assertions": [
            {"description": a.description, "passed": a.passed, "detail": a.detail}
            for a in result.assertions
        ],
        "observations": result.observations,
        "runs": [
            {
                "argv": r.argv,
                "returncode": r.returncode,
                "timed_out": r.timed_out,
                "duration_s": round(r.duration_s, 2),
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
            for r in result.runs
        ],
    }


def status_counts(results: list[ScenarioResult]) -> dict[str, int]:
    """Return a ``{status: count}`` tally across all results."""
    counts = Counter(r.status.value for r in results)
    return {s.value: counts.get(s.value, 0) for s in Status}


def to_json(results: list[ScenarioResult], *, meta: dict) -> str:
    """Render the full run as a JSON string.

    Args:
        results: All scenario results.
        meta: Run-level metadata (prefix, uri, timestamp, git sha, …).

    Returns:
        A pretty-printed JSON document.
    """
    return json.dumps(
        {
            "meta": meta,
            "summary": status_counts(results),
            "scenarios": [_result_to_dict(r) for r in results],
        },
        indent=2,
    )


def human_summary(results: list[ScenarioResult]) -> str:
    """Render a plain-text recap (status glyph, name, failed-assertion detail)."""
    glyph = {Status.PASS: "✓", Status.FAIL: "✗", Status.ERROR: "!", Status.SKIP: "-"}
    lines: list[str] = []
    for r in results:
        lines.append(
            f"  {glyph[r.status]} {r.status.value.upper():5} {r.name}  ({r.duration_s:.1f}s)"
        )
        for a in r.assertions:
            if not a.passed:
                lines.append(f"        ✗ {a.description} — {a.detail}")
        if r.error:
            lines.append(f"        ! {r.error}")
        for obs in r.observations:
            lines.append(f"        · {obs}")
    counts = status_counts(results)
    tally = "  ".join(f"{k}={v}" for k, v in counts.items())
    lines.append("")
    lines.append(f"  {tally}")
    return "\n".join(lines)


def issue_markdown(results: list[ScenarioResult], *, meta: dict) -> str:
    """Render a GitHub issue body + sub-issue stubs for failures (not auto-filed).

    Args:
        results: All scenario results.
        meta: Run-level metadata.

    Returns:
        Markdown suitable for pasting into a tracking issue.
    """
    counts = status_counts(results)
    out: list[str] = [
        f"# 0.6.0 CLI conformance run — {meta.get('timestamp', 'n/a')}",
        "",
        f"- Build: `{meta.get('git_describe', 'n/a')}`  ·  URI: `{meta.get('uri')}`",
        f"- Prefix: `{meta.get('prefix')}`",
        f"- Result: **{counts['pass']} pass / {counts['fail']} fail / "
        f"{counts['error']} error / {counts['skip']} skip**",
        "",
        "| Scenario | Lane | Tags | Status |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        out.append(f"| {r.name} | {r.needs} | {' '.join(r.tags)} | {r.status.value} |")

    failures = [r for r in results if r.status in (Status.FAIL, Status.ERROR)]
    if failures:
        out += ["", "## Proposed sub-issues (failures)", ""]
        for r in failures:
            detail = r.error or "; ".join(
                f"{a.description} ({a.detail})" for a in r.assertions if not a.passed
            )
            out += [
                f"### `{r.name}` — {r.status.value}",
                f"- Tags: {' '.join(r.tags) or '—'}",
                f"- Evidence: {detail}",
                "",
            ]

    observed = [(r, o) for r in results for o in r.observations if "#" in o]
    if observed:
        out += ["## Observations (known-issue signals, not failures)", ""]
        for r, o in observed:
            out.append(f"- `{r.name}`: {o}")

    return "\n".join(out)
