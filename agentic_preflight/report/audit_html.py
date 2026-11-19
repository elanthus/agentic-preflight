"""Render the audit dictionary from :mod:`.audit` as one self-contained HTML page.

The page has inline CSS and nothing else: no script, font, image, or network
request, and a Content-Security-Policy that forbids them. Every value from the
run is untrusted text and passes through :func:`esc` before it reaches markup;
no value is ever placed in a tag name, an attribute name, a URL, or a style.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

#: Tones carry a glyph and a word as well as a color, so status never depends on color.
_GLYPHS = {"ok": "\u2713", "bad": "\u2715", "warn": "!", "neutral": "\u2013", "info": "\u2022"}

_STAGE_STATUS = {
    "green": ("ok", "Passed"),
    "red": ("bad", "Failed"),
    "skipped": ("neutral", "Skipped"),
    "pending": ("warn", "Pending"),
    "not_reached": ("neutral", "Not reached"),
}

_FINDING_STATUS = {
    "fixed": ("ok", "Fixed"),
    "dismissed": ("neutral", "Dismissed"),
    "accepted": ("warn", "Accepted risk"),
    "open": ("bad", "Open"),
}

_SEVERITY = {
    "critical": ("bad", "Critical"),
    "high": ("bad", "High"),
    "medium": ("warn", "Medium"),
    "low": ("neutral", "Low"),
}

_GOOD_STATES = {"DONE", "PUSHED", "VERIFIED", "AWAITING_PUSH_CONFIRM"}
_ENDED_STATES = {"ABORTED", "ORPHANED"}
_BAD_MARKERS = ("_RED", "_BLOCKED", "_CONFLICT", "SETUP_FAILED")

_REASONS = {
    "false_positive": "false positive",
    "out_of_scope": "out of scope",
    "duplicate": "duplicate",
    "already_fixed": "already fixed",
    "accepted_risk": "accepted risk",
    "deferred": "deferred",
    "other": "other",
}

_SOURCES = {
    "flag": "stated with a command-line flag",
    "environment": "read from an environment marker the agent sets",
    "command_output": "printed by the reviewer process",
    None: "not stated",
}


def esc(value: object) -> str:
    """Escape any value for use as element text or a quoted attribute value."""
    return html.escape("" if value is None else str(value), quote=True)


def _badge(tone: str, text: str, *, extra: str = "") -> str:
    return (
        f'<span class="badge badge--{tone}{extra}">'
        f'<span class="badge__glyph" aria-hidden="true">{_GLYPHS[tone]}</span>{esc(text)}</span>'
    )


def _state_tone(state: str) -> str:
    if state in _GOOD_STATES:
        return "ok"
    if state in _ENDED_STATES:
        return "neutral"
    if any(marker in state for marker in _BAD_MARKERS):
        return "bad"
    return "info"


def _when(value: object) -> str:
    if not isinstance(value, str):
        return '<span class="muted">not recorded</span>'
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return esc(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    shown = parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f'<time datetime="{esc(value)}">{esc(shown)}</time>'


def _duration(seconds: object) -> str:
    if not isinstance(seconds, int | float):
        return '<span class="muted">not recorded</span>'
    if seconds == 0:
        return "under 1 s"
    if seconds < 10 and seconds != int(seconds):
        return esc(f"{seconds:.2f} s")
    if seconds < 60:
        return esc(f"{seconds:.0f} s")
    minutes, rest = divmod(int(seconds), 60)
    return esc(f"{minutes} min {rest} s")


def _hash(value: object, *, short: bool = False) -> str:
    if not isinstance(value, str) or not value:
        return '<span class="muted">none</span>'
    shown = value[:12] if short else value
    return f'<code class="hash" title="{esc(value)}">{esc(shown)}</code>'


def _text(value: object, empty: str = "none") -> str:
    if value is None or value == "":
        return f'<span class="muted">{esc(empty)}</span>'
    return esc(value)


def _facts(rows: Iterable[tuple[str, str]], cls: str = "facts") -> str:
    items = "".join(f"<div><dt>{esc(label)}</dt><dd>{value}</dd></div>" for label, value in rows)
    return f'<dl class="{cls}">{items}</dl>'


def _json_block(value: object) -> str:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    return f'<pre class="raw">{esc(text)}</pre>'


def _section(anchor: str, title: str, body: str, lede: str | None = None) -> str:
    intro = f'<p class="lede">{lede}</p>' if lede else ""
    return (
        f'<section id="{anchor}" aria-labelledby="{anchor}-h">'
        f'<h2 id="{anchor}-h">{esc(title)}</h2>{intro}{body}</section>'
    )


# -- header -----------------------------------------------------------------


def _masthead(data: Mapping[str, Any]) -> str:
    run = data["run"]
    banner = data["chain"]["banner"]
    state = str(run["state"])
    head = run.get("pushed_sha") or run["head_sha"]
    facts = _facts(
        [
            ("Branch", f"<code>{esc(run['branch'])}</code>"),
            (
                "Base",
                f"<code>{esc(run['base_ref'])}</code> at {_hash(run['merge_base_sha'], short=True)}",
            ),
            ("Head commit", _hash(head)),
            ("Run", f"<code>{esc(run['run_id'])}</code>"),
            (
                # A report can be made while the run is still going.
                "Final state" if data["summary"].get("terminal") else "Current state",
                _badge(_state_tone(state), state),
            ),
            ("Started", _when(run.get("created_at"))),
            ("Last updated", _when(run.get("updated_at"))),
        ]
    )
    summary = data["summary"]
    counts = summary["findings"]
    tally = ", ".join(f"{counts[key]} {key}" for key in ("fixed", "dismissed", "accepted", "open"))
    seal = (
        f'<div class="seal seal--{esc(banner["tone"])}" role="status">'
        f'<span class="seal__glyph" aria-hidden="true">{_GLYPHS[banner["tone"]]}</span>'
        f'<div><p class="seal__headline">{esc(banner["headline"])}</p>'
        f'<p class="seal__detail">{esc(banner["detail"])}</p></div></div>'
    )
    return (
        '<header class="masthead">'
        '<p class="kicker">Agentic Preflight audit trail</p>'
        f'<h1><span class="repo">{esc(run["repository"])}</span>'
        f'<span class="branch">{esc(run["branch"])}</span></h1>'
        f'<p class="tally">{esc(summary["findings_total"])} findings: {esc(tally)}.</p>'
        f"{seal}{facts}{_shareable(data)}</header>"
    )


def _shareable(data: Mapping[str, Any]) -> str:
    if data["local_details"]:
        return (
            '<p class="notice notice--warn"><strong>Local details included.</strong> '
            "This copy holds local paths and user-level configuration. "
            "Check it before you share it.</p>"
        )
    items = "".join(f"<li>{esc(item)}</li>" for item in data["withheld"])
    return (
        '<div class="notice"><p><strong>Shareable view.</strong> Left out of this copy:</p>'
        f"<ul>{items}</ul>"
        "<p>Run <code>agentic-preflight audit --local-details</code> to include them.</p></div>"
    )


_TOC = (
    ("proves", "What this proves"),
    ("intent", "Intent"),
    ("actors", "Who acted"),
    ("stages", "Stages"),
    ("findings", "Findings"),
    ("timeline", "Timeline"),
    ("integrity", "Integrity"),
)


def _toc() -> str:
    links = "".join(f'<li><a href="#{anchor}">{esc(title)}</a></li>' for anchor, title in _TOC)
    return f'<nav class="toc" aria-label="Sections"><ol>{links}</ol></nav>'


# -- body sections ----------------------------------------------------------


def _proves(data: Mapping[str, Any]) -> str:
    shows = "".join(f"<li>{esc(item)}</li>" for item in data["proves"])
    not_shown = "".join(f"<li>{esc(item)}</li>" for item in data["does_not_prove"])
    body = (
        '<div class="split">'
        f'<div class="split__col split__col--yes"><h3>A completed run shows</h3><ul>{shows}</ul></div>'
        f'<div class="split__col split__col--no"><h3>It does not show</h3><ul>{not_shown}</ul></div>'
        "</div>"
    )
    return _section("proves", "What this record proves", body)


def _intent(data: Mapping[str, Any]) -> str:
    run = data["run"]
    intent = run.get("intent")
    quote = (
        f'<blockquote class="intent">{esc(intent)}</blockquote>'
        if intent
        else '<p class="muted">The run records no intent.</p>'
    )
    files = run.get("changed_files") or []
    coverage = run.get("review_coverage") or {}
    risk = run.get("risk") or {}
    facts = _facts(
        [
            ("Source", _text(run.get("intent_source"))),
            ("Changed files", esc(len(files))),
            ("Review units", _coverage(coverage)),
            ("Risk", _text(risk.get("level"), "not assessed")),
            ("Verdict", _text(risk.get("verdict"), "not assessed")),
            ("Validation restarts", esc(run.get("validation_restarts", 0))),
        ],
        "facts facts--compact",
    )
    listing = ""
    if files:
        shown = "".join(f"<li><code>{esc(name)}</code></li>" for name in files)
        listing = f'<details class="more"><summary>Changed files</summary><ul class="files">{shown}</ul></details>'
    lede = (
        "The user's objective as the agent recorded it. It is the agent's text, quoted as written."
    )
    return _section("intent", "Intent", quote + facts + listing, lede)


def _coverage(coverage: Mapping[str, Any]) -> str:
    if not coverage:
        return '<span class="muted">not recorded</span>'
    return esc(
        f"{coverage.get('total_units', 0)} total, {coverage.get('cited_count', 0)} cited, "
        f"{coverage.get('clean_count', 0)} clean"
    )


def _provenance(item: Mapping[str, Any]) -> str:
    if item["kind"] == "measured":
        return _badge("info", "Measured", extra=" badge--measured")
    source = _SOURCES.get(item.get("source"), str(item.get("source")))
    return (
        f"{_badge('warn', 'Self-reported', extra=' badge--claim')}"
        f'<span class="source">{esc(source)}</span>'
    )


def _claim_rows(items: Iterable[Mapping[str, Any]]) -> str:
    rows = []
    for item in items:
        cls = "claim" if item["kind"] == "claimed" else "measure"
        value = (
            f'<span class="value value--{cls}">{esc(item["value"])}</span>'
            if item.get("value") not in (None, "")
            else '<span class="muted">not stated</span>'
        )
        rows.append(
            f'<div class="row row--{cls}"><dt>{esc(item["label"])}</dt>'
            f"<dd>{value}{_provenance(item)}</dd></div>"
        )
    return f'<dl class="ledger">{"".join(rows)}</dl>'


def _actors(data: Mapping[str, Any]) -> str:
    actors = data["actors"]
    producer = actors["producer"]
    parts = ['<div class="actor"><h3>Producer</h3>']
    parts.append(
        _claim_rows(producer["items"])
        if producer["recorded"]
        else f'<p class="muted">{esc(producer["note"])}</p>'
    )
    parts.append("</div>")
    parts.append(_reviewer_block(actors["reviewer"]))
    parts.append(_authorization_block(actors["authorizations"]))
    legend = (
        f"{_badge('info', 'Measured', extra=' badge--measured')} values were derived by the "
        f"tool's own code. {_badge('warn', 'Self-reported', extra=' badge--claim')} values are "
        "what an agent or reviewer said about itself; nothing checked them."
    )
    return _section("actors", "Who acted", f'<div class="actors">{"".join(parts)}</div>', legend)


def _reviewer_block(reviewer: Mapping[str, Any]) -> str:
    parts = ['<div class="actor"><h3>Reviewer</h3>', _claim_rows([reviewer["executor"]])]
    if not reviewer["reports"]:
        parts.append(
            '<p class="muted">No reviewer report recorded. An in-harness review, a reviewer '
            "that reports nothing, or a run from an earlier release leaves none.</p>"
        )
    for report in reviewer["reports"]:
        times = ", ".join(_when(at) for at in report["at"])
        parts.append(f'<p class="sub">Reported at {times}</p>')
        parts.append(_claim_rows(report["items"]))
        prompts = "".join(
            f"<li>{esc(prompt.get('phase'))} {_hash(prompt.get('sha256'), short=True)}</li>"
            for prompt in report["prompts"]
        )
        omitted = report.get("prompts_omitted") or 0
        more = f"<li>and {esc(omitted)} more</li>" if omitted else ""
        if prompts or more:
            parts.append(
                '<p class="sub">Prompt digests (self-reported)</p>'
                f'<ul class="prompts">{prompts}{more}</ul>'
            )
    parts.extend(
        f'<p class="notice notice--warn">A reviewer report was ignored at '
        f"{_when(ignored['at'])}: {esc(ignored['reason'])}.</p>"
        for ignored in reviewer["ignored"]
    )
    parts.append("</div>")
    return "".join(parts)


def _authorization_block(authorizations: list[Mapping[str, Any]]) -> str:
    parts = ['<div class="actor"><h3>Push authorization</h3>']
    if not authorizations:
        parts.append(
            '<p class="muted">No push was authorized in this run, or the run predates '
            "recorded authorizations.</p>"
        )
    for item in authorizations:
        parts.append(
            f'<p class="sub">For {_hash(item["sha"], short=True)} at {_when(item["at"])}</p>'
        )
        parts.append(_claim_rows([item["claim"]]))
    parts.append("</div>")
    return "".join(parts)


_STAGE_ROWS: tuple[tuple[str, str, Any], ...] = (
    ("Reason", "reason", esc),
    ("Command", "command", lambda v: f"<code>{esc(v)}</code>"),
    ("Exit code", "exit_code", esc),
    ("Output digest", "output_sha256", lambda v: _hash(v, short=True)),
    ("Command time", "command_seconds", _duration),
    ("Started", "started_at", _when),
    ("Stage time", "duration_seconds", _duration),
    ("Commit", "head_sha", lambda v: _hash(v, short=True)),
)


def _stage_card(stage: Mapping[str, Any]) -> str:
    tone, label = _STAGE_STATUS.get(stage["status"], ("neutral", str(stage["status"])))
    title = f"<h3>{esc(str(stage['name']).capitalize())}</h3>{_badge(tone, label)}"
    if not stage.get("reached"):
        return f'<article class="stage stage--{tone}"><header>{title}</header></article>'
    rows: list[tuple[str, str]] = []
    if stage["name"] == "review":
        rows.append(("Executor", _text(stage.get("executor"), "not recorded")))
    rows.extend(
        (label, show(stage[key])) for label, key, show in _STAGE_ROWS if stage.get(key) is not None
    )
    rows.append(("Attempts", esc(stage.get("attempts"))))
    if stage.get("reused_from"):
        origin = stage["reused_from"]
        rows.append(
            (
                "Evidence",
                f"reused from run <code>{esc(origin['run_id'])}</code> at {_hash(origin['head_sha'], short=True)}",
            )
        )
    if stage.get("log_path"):
        rows.append(("Log", f"<code>{esc(stage['log_path'])}</code>"))
    return (
        f'<article class="stage stage--{tone}"><header>{title}</header>'
        f"{_facts(rows, 'facts facts--stage')}</article>"
    )


def _stages(data: Mapping[str, Any]) -> str:
    cards = "".join(_stage_card(stage) for stage in data["stages"])
    lede = "Status, exit codes, and digests are recorded by the tool, not reported by the agent."
    return _section("stages", "Stages", f'<div class="stages">{cards}</div>', lede)


def _resolution(item: Mapping[str, Any]) -> str:
    parts = []
    if item.get("fix_commit"):
        subject = f" {esc(item['fix_subject'])}" if item.get("fix_subject") else ""
        parts.append(f"Fixed by {_hash(item['fix_commit'], short=True)}{subject}")
    if item.get("reason"):
        parts.append(f"Reason: {esc(_REASONS.get(item['reason'], item['reason']))}")
    if item.get("resolved_at"):
        parts.append(f"Resolved {_when(item['resolved_at'])}")
    lines = "".join(f"<li>{part}</li>" for part in parts)
    note = f'<blockquote class="note">{esc(item["note"])}</blockquote>' if item.get("note") else ""
    if not lines and not note:
        return ""
    return f'<div class="resolution"><ul>{lines}</ul>{note}</div>'


def _finding(item: Mapping[str, Any]) -> str:
    tone, label = _FINDING_STATUS.get(item["status"], ("neutral", str(item["status"])))
    sev_tone, sev_label = _SEVERITY.get(item["severity"], ("neutral", str(item["severity"])))
    detail = f'<p class="detail">{esc(item["detail"])}</p>' if item.get("detail") else ""
    suggestion = (
        f'<details class="more"><summary>Suggestion</summary><p>{esc(item["suggestion"])}</p></details>'
        if item.get("suggestion")
        else ""
    )
    return (
        f'<article class="finding finding--{tone}">'
        f'<header><span class="fid">{esc(item["id"])}</span>'
        f"<h3>{esc(item['title'])}</h3></header>"
        f'<p class="meta">{_badge(tone, label)}{_badge(sev_tone, sev_label, extra=" badge--sev")}'
        f'<code class="loc">{esc(item["location"])}</code>'
        f'<span class="muted">{esc(item["stage"])} stage</span></p>'
        f"{detail}{suggestion}{_resolution(item)}</article>"
    )


def _findings(data: Mapping[str, Any]) -> str:
    findings = data["findings"]
    parts = []
    if findings.get("error"):
        parts.append(f'<p class="notice notice--bad">{esc(findings["error"])}</p>')
    if not findings["items"] and not findings.get("error"):
        parts.append('<p class="muted">No findings were recorded.</p>')
    parts.extend(_finding(item) for item in findings["items"])
    lede = "Titles, details, and notes are the agent's words. Statuses and fix commits are recorded by the tool."
    return _section("findings", "Findings", "".join(parts), lede)


def _timeline(data: Mapping[str, Any]) -> str:
    timeline = data["timeline"]
    parts = []
    if timeline.get("error"):
        parts.append(
            f'<p class="notice notice--bad">The event log could not be read: {esc(timeline["error"])}</p>'
        )
    if timeline.get("skipped_lines"):
        parts.append(
            f'<p class="notice notice--warn">{esc(timeline["skipped_lines"])} line(s) of the '
            "log are not JSON objects, such as a torn write, and are not shown.</p>"
        )
    if not timeline["events"]:
        parts.append('<p class="muted">No events were recorded.</p>')
    else:
        items = "".join(
            f'<li class="ev ev--{esc(event["category"])}"><details>'
            f'<summary><span class="ev__time">{_when(event["at"])}</span>'
            f'<span class="ev__text">{esc(event["summary"])}</span>'
            f'<span class="ev__more">event {esc(event["index"])}</span></summary>'
            f"{_json_block(event['raw'])}</details></li>"
            for event in timeline["events"]
        )
        parts.append(f'<ol class="timeline">{items}</ol>')
    lede = (
        "Every event in the run's log, oldest first. Select an entry to read it exactly as stored."
    )
    return _section("timeline", "Timeline", "".join(parts), lede)


def _chain_block(chain: Mapping[str, Any]) -> str:
    rows = [
        ("Status", _badge(chain["banner"]["tone"], chain["banner"]["headline"])),
        ("Lines", _text(chain.get("lines"), "unknown")),
        ("Chained lines", _text(chain.get("chained_lines"), "unknown")),
        ("Unchained lines", _text(chain.get("unchained_lines"), "unknown")),
        ("Torn lines", _text(chain.get("torn_lines"), "unknown")),
        ("Last line digest", _hash(chain.get("head_sha256"))),
    ]
    breaks = chain.get("breaks") or []
    listing = ""
    if breaks:
        items = "".join(
            f"<li>Line {esc(item.get('line'))}: {esc(item.get('reason'))}</li>" for item in breaks
        )
        listing = f'<p class="sub">Breaks</p><ul class="breaks">{items}</ul>'
    return f'<div class="actor"><h3>Event log chain</h3>{_facts(rows)}{listing}</div>'


def _verification(verification: Mapping[str, Any] | None) -> str:
    """Whether the note verified in this clone when the report was made."""
    if not verification:
        return _badge("neutral", "Not checked")
    if verification.get("ok") is True:
        return f"{_badge('ok', 'Verifies in this clone')}<br>As <code>verify</code> checks it, offline."
    detail = esc(verification.get("detail"))
    if verification.get("ok") is False:
        reason = esc(verification.get("reason"))
        return f"{_badge('bad', 'Does not verify')}<br><code>{reason}</code>: {detail}"
    return f"{_badge('warn', 'Could not be checked')}<br>{detail}"


def _attestation_block(note: Mapping[str, Any]) -> str:
    head = f'<div class="actor"><h3>Attestation note</h3><p class="sub">On {_hash(note["sha"], short=True)}</p>'
    if note.get("present") is None:
        return f'{head}<p class="notice notice--warn">{esc(note.get("error"))}</p></div>'
    if not note.get("present"):
        return f'{head}<p class="muted">The commit has no attestation note in this clone.</p></div>'
    if not note.get("valid"):
        return f'{head}<p class="notice notice--bad">The note is present but invalid: {esc(note.get("error"))}</p></div>'
    stages = ", ".join(f"{name} {status}" for name, status in (note.get("stages") or {}).items())
    summary = ", ".join(
        f"{count} {key}" for key, count in (note.get("findings_summary") or {}).items()
    )
    rows = [
        ("Verification", _verification(note.get("verification"))),
        # The note's own word for its result. It is not the outcome of a check made here.
        ("Recorded outcome", f"<code>{esc(note.get('outcome'))}</code>"),
        ("Schema", esc(note.get("schema_version"))),
        (
            "Run",
            _badge("ok", "This run")
            if note.get("same_run")
            else f"<code>{esc(note.get('run_id'))}</code>",
        ),
        ("Green at", _when(note.get("green_at"))),
        ("Stages", _text(stages)),
        ("Findings", _text(summary)),
        ("Config digest", _hash(note.get("config_sha256"), short=True)),
    ]
    return f'{head}{_facts(rows)}<p class="muted">A note is a record, not a signature.</p></div>'


def _commit_list(commits: Iterable[Mapping[str, Any]]) -> str:
    items = "".join(
        f"<li>{_hash(commit['sha'], short=True)} {_text(commit.get('subject'), '')}</li>"
        for commit in commits
    )
    return f'<ul class="commits">{items}</ul>' if items else '<p class="muted">None.</p>'


def _publication_block(publication: Mapping[str, Any]) -> str:
    parts = ['<div class="actor"><h3>Publication</h3>']
    if not publication["gates"]:
        parts.append(
            '<p class="muted">No gate listing recorded: the run never reached the gate, or '
            "predates recorded gate listings.</p>"
        )
    for gate in publication["gates"]:
        parts.append(f'<p class="sub">The gate at {_when(gate["at"])} listed</p>')
        parts.append(_commit_list(gate["commits"]))
        notes = gate.get("published_notes") or []
        if notes:
            parts.append(f'<p class="sub">Notes to publish: {esc(len(notes))}</p>')
    pushes = publication["pushes"]
    if pushes:
        items = "".join(
            f"<li>{_hash(p['sha'], short=True)} at {_when(p['at'])}</li>" for p in pushes
        )
        parts.append(f'<p class="sub">Pushed</p><ul class="commits">{items}</ul>')
    else:
        parts.append(
            '<p class="sub">Pushed</p><p class="muted">Nothing was pushed by this run.</p>'
        )
    if publication["fix_commits"]:
        parts.append('<p class="sub">Fix commits</p>')
        parts.append(_commit_list(publication["fix_commits"]))
    parts.append("</div>")
    return "".join(parts)


def _config_block(run: Mapping[str, Any]) -> str:
    return (
        '<div class="actor actor--wide"><h3>Configuration</h3>'
        f"{_facts([('Digest', _hash(run.get('config_digest')))])}"
        '<details class="more"><summary>Configuration snapshot</summary>'
        f"{_json_block(run.get('config_snapshot'))}</details></div>"
    )


def _integrity(data: Mapping[str, Any]) -> str:
    body = (
        '<div class="actors">'
        f"{_chain_block(data['chain'])}"
        f"{_attestation_block(data['publication']['attestation'])}"
        f"{_publication_block(data['publication'])}"
        f"{_config_block(data['run'])}"
        "</div>"
    )
    return _section("integrity", "Integrity and publication", body)


def _footer(data: Mapping[str, Any]) -> str:
    return (
        '<footer class="colophon">'
        f"<p>Generated by {esc(data['generator'])} at {_when(data['generated_at'])}, "
        f"audit format {esc(data['audit_version'])}. This file is a rendering of local run "
        "records; it is not signed.</p></footer>"
    )


def render(data: Mapping[str, Any]) -> str:
    """Return the complete HTML document for an audit dictionary."""
    run = data["run"]
    title = f"Audit: {run['repository']} {run['branch']} ({run['run_id']})"
    body = "".join(
        [
            _masthead(data),
            _toc(),
            "<main>",
            _proves(data),
            _intent(data),
            _actors(data),
            _stages(data),
            _findings(data),
            _timeline(data),
            _integrity(data),
            "</main>",
            _footer(data),
        ]
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="referrer" content="no-referrer">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'\">"
        f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
        f'<body><div class="page">{body}</div></body></html>\n'
    )


STYLE = """
:root{
  --paper:#f5f6f3;--sheet:#ffffff;--ink:#1d2430;--muted:#5d6675;--rule:#d9ddd6;
  --accent:#23466e;--accent-soft:#e6edf5;
  --ok:#006e52;--ok-soft:#e1f2ec;--bad:#a53a00;--bad-soft:#fbe9df;
  --warn:#7d5300;--warn-soft:#f8efd9;--neutral:#545c68;--neutral-soft:#eceeea;
  --info:#23466e;--info-soft:#e6edf5;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Charter,"Bitstream Charter",Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#12161c;--sheet:#1a1f27;--ink:#e4e7ea;--muted:#9aa3ae;--rule:#2d343e;
  --accent:#9cbce3;--accent-soft:#1f2b3a;
  --ok:#5fd0a8;--ok-soft:#14302a;--bad:#ff9a6b;--bad-soft:#3a2219;
  --warn:#e9be62;--warn-soft:#352b15;--neutral:#aab2bc;--neutral-soft:#252b33;
  --info:#9cbce3;--info-soft:#1f2b3a;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
.page{max-width:62rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
h1,h2,h3{font-family:var(--serif);font-weight:600;line-height:1.2;margin:0}
h2{font-size:1.6rem;margin:0 0 .4rem;padding-top:.25rem}
h3{font-size:1.1rem}
code,pre,.hash{font-family:var(--mono);font-size:.86em}
code{overflow-wrap:anywhere}
.hash{word-break:break-all}
a{color:var(--accent)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.muted{color:var(--muted)}
.kicker{margin:0 0 .5rem;color:var(--muted);font-size:.95rem}
.masthead h1{font-size:clamp(1.9rem,5vw,2.8rem);display:flex;flex-wrap:wrap;gap:.2rem .7rem;align-items:baseline}
.masthead .branch{font-family:var(--mono);font-size:.5em;font-weight:400;color:var(--muted);overflow-wrap:anywhere}
.tally{margin:.4rem 0 1.4rem;color:var(--muted)}
.seal{display:flex;gap:1rem;align-items:flex-start;padding:1.1rem 1.25rem;border:2px solid currentColor;border-radius:.5rem;margin:0 0 1.5rem;background:var(--sheet)}
.seal p{margin:0;color:var(--ink)}
.seal__glyph{flex:none;display:grid;place-items:center;width:2.6rem;height:2.6rem;border-radius:50%;border:2px solid currentColor;font-size:1.3rem;font-weight:700}
.seal__headline{font-family:var(--serif);font-size:1.35rem;font-weight:600}
.seal__detail{margin-top:.2rem!important;max-width:44rem}
.seal--ok{color:var(--ok)}.seal--bad{color:var(--bad)}.seal--warn{color:var(--warn)}
.seal--bad{border-width:3px;background:var(--bad-soft)}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(13rem,1fr));gap:.9rem 1.5rem;margin:0}
.facts div{min-width:0}
.facts dt{font-size:.8rem;color:var(--muted)}
.facts dd{margin:.1rem 0 0;min-width:0;overflow-wrap:anywhere}
.facts--compact{margin-top:1rem}
.facts--stage{grid-template-columns:1fr;gap:.45rem}
.facts--stage div{display:grid;grid-template-columns:7.5rem minmax(0,1fr);gap:.5rem}
.facts--stage dd{margin:0}
.notice{margin:1.25rem 0 0;padding:.75rem 1rem;border-left:4px solid var(--accent);background:var(--accent-soft);border-radius:0 .35rem .35rem 0;font-size:.93rem}
.notice p{margin:.2rem 0}.notice ul{margin:.3rem 0;padding-left:1.2rem}
.notice--warn{border-color:var(--warn);background:var(--warn-soft)}
.notice--bad{border-color:var(--bad);background:var(--bad-soft)}
.toc{margin:2rem 0 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.toc ol{list-style:none;margin:0;padding:.6rem 0;display:flex;flex-wrap:wrap;gap:.3rem 1.3rem;font-size:.93rem}
section{padding:2.4rem 0 .4rem;border-bottom:1px solid var(--rule)}
section:last-child{border-bottom:0}
.lede{margin:0 0 1.2rem;color:var(--muted);max-width:44rem}
.split{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem;margin-top:1rem}
.split__col{background:var(--sheet);border:1px solid var(--rule);border-radius:.5rem;padding:1rem 1.2rem}
.split__col h3{margin-bottom:.5rem}
.split__col ul{margin:0;padding-left:1.1rem}
.split__col li{margin:.35rem 0}
.split__col--yes{border-top:4px solid var(--ok)}
.split__col--no{border-top:4px solid var(--bad)}
.intent{margin:0;padding:1rem 1.25rem;font-family:var(--serif);font-size:1.2rem;line-height:1.5;background:var(--sheet);border-left:4px solid var(--accent);border-radius:0 .5rem .5rem 0;white-space:pre-wrap;overflow-wrap:anywhere;max-width:48rem}
.actors{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1.25rem}
.actor{background:var(--sheet);border:1px solid var(--rule);border-radius:.5rem;padding:1rem 1.2rem;min-width:0}
.actor--wide{grid-column:1/-1}
.actor h3{margin-bottom:.6rem}
.sub{margin:.9rem 0 .3rem;font-size:.85rem;color:var(--muted)}
.ledger{margin:0}
.ledger .row{display:grid;grid-template-columns:minmax(7rem,38%) 1fr;gap:.6rem;padding:.45rem 0;border-top:1px solid var(--rule)}
.ledger .row:first-child{border-top:0}
.ledger dt{color:var(--muted);font-size:.9rem}
.ledger dd{margin:0;display:flex;flex-wrap:wrap;gap:.25rem .5rem;align-items:center;min-width:0}
.value{overflow-wrap:anywhere}
.value--claim{font-style:italic;text-decoration:underline dashed var(--warn);text-underline-offset:3px}
.value--measure{font-weight:600}
.source{font-size:.8rem;color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:.3rem;padding:.08rem .5rem;border-radius:1rem;font-size:.8rem;font-weight:600;line-height:1.5;border:1px solid currentColor;white-space:nowrap}
.badge__glyph{font-weight:700}
.badge--ok{color:var(--ok);background:var(--ok-soft)}
.badge--bad{color:var(--bad);background:var(--bad-soft)}
.badge--warn{color:var(--warn);background:var(--warn-soft)}
.badge--neutral{color:var(--neutral);background:var(--neutral-soft)}
.badge--info{color:var(--info);background:var(--info-soft)}
.badge--claim{border-style:dashed;background:transparent}
.badge--measured{background:var(--info-soft)}
.badge--sev{font-weight:500}
.stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(22rem,1fr));gap:1rem}
.stage{background:var(--sheet);border:1px solid var(--rule);border-top:4px solid var(--neutral);border-radius:.5rem;padding:.9rem 1rem;min-width:0;font-size:.92rem}
.stage header{display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.7rem}
.stage--ok{border-top-color:var(--ok)}.stage--bad{border-top-color:var(--bad)}.stage--warn{border-top-color:var(--warn)}
.stage .facts dt{font-size:.8rem}
.finding{background:var(--sheet);border:1px solid var(--rule);border-left:4px solid var(--neutral);border-radius:.5rem;padding:1rem 1.2rem;margin:0 0 1rem}
.finding--ok{border-left-color:var(--ok)}.finding--bad{border-left-color:var(--bad)}.finding--warn{border-left-color:var(--warn)}
.finding header{display:flex;gap:.75rem;align-items:baseline}
.finding h3{overflow-wrap:anywhere}
.fid{font-family:var(--mono);font-size:.85rem;color:var(--muted);flex:none}
.finding .meta{display:flex;flex-wrap:wrap;gap:.4rem .6rem;align-items:center;margin:.5rem 0 0;font-size:.88rem}
.loc{overflow-wrap:anywhere}
.detail{margin:.7rem 0 0;max-width:46rem;white-space:pre-wrap;overflow-wrap:anywhere}
.resolution{margin-top:.8rem;padding-top:.7rem;border-top:1px dashed var(--rule);font-size:.92rem}
.resolution ul{margin:0;padding-left:1.1rem}
.note{margin:.5rem 0 0;padding:.4rem .9rem;border-left:3px solid var(--rule);color:var(--ink);font-style:italic;white-space:pre-wrap;overflow-wrap:anywhere}
.more{margin-top:.7rem}
.more summary,.ev__raw summary{cursor:pointer;color:var(--accent);font-size:.88rem}
.files,.commits,.prompts,.breaks{margin:.3rem 0 0;padding-left:1.1rem;font-size:.9rem}
.files li,.commits li{overflow-wrap:anywhere}
.timeline{list-style:none;margin:0;padding:0;border-left:2px solid var(--rule)}
.ev{position:relative;padding:0 0 0 1.4rem}
.ev::before{content:"";position:absolute;left:-.45rem;top:.85rem;width:.7rem;height:.7rem;border-radius:50%;background:var(--sheet);border:2px solid var(--neutral)}
.ev summary{display:grid;grid-template-columns:12.5rem 1fr auto;gap:.2rem 1rem;align-items:baseline;padding:.45rem .5rem;margin-left:-.5rem;border-radius:.35rem;cursor:pointer;list-style:none}
.ev summary::-webkit-details-marker{display:none}
.ev summary:hover,.ev details[open] summary{background:var(--accent-soft)}
.ev--state::before{border-color:var(--accent)}
.ev--finding::before{border-color:var(--warn);border-radius:2px}
.ev--claim::before{border-style:dashed;border-color:var(--warn)}
.ev--publish::before{background:var(--ok);border-color:var(--ok)}
.ev--trace::before{width:.45rem;height:.45rem;left:-.33rem;top:.95rem}
.ev--trace .ev__text{color:var(--muted);font-size:.92rem}
.ev__time{font-family:var(--mono);font-size:.8rem;color:var(--muted)}
.ev__text{overflow-wrap:anywhere}
.ev--finding .ev__text,.ev--claim .ev__text,.ev--publish .ev__text{font-weight:600}
.ev__more{font-size:.78rem;color:var(--accent);white-space:nowrap}
.ev .raw{margin:.2rem 0 .6rem}
.raw{margin:.4rem 0 0;padding:.75rem;background:var(--paper);border:1px solid var(--rule);border-radius:.35rem;overflow-x:auto;font-size:.8rem;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}
.colophon{margin-top:2.5rem;color:var(--muted);font-size:.85rem}
@media (max-width:40rem){
  .page{padding:1.5rem 1rem 3rem}
  .split{grid-template-columns:1fr}
  .stages{grid-template-columns:1fr}
  .ev summary{grid-template-columns:1fr auto}
  .ev__time{grid-column:1/-1}
  .seal{padding:.9rem 1rem}
  .ledger .row{grid-template-columns:1fr}
}
@media print{
  :root{--paper:#fff;--sheet:#fff;--ink:#000;--muted:#444;--rule:#bbb}
  body{font-size:10.5pt}
  .page{max-width:none;padding:0}
  .toc,.ev__more,.ev .raw,.more{display:none}
  section{break-inside:auto;padding-top:1.2rem}
  .finding,.stage,.actor,.seal,.split__col,.ev{break-inside:avoid}
  h2{break-after:avoid}
  a{color:inherit;text-decoration:none}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""
