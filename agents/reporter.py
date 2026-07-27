"""
Reporter Agent
--------------
Final pipeline step that runs after Scribe.

Reads every artifact Scribe produced (plus the underlying pipeline data —
sources, gaps, proposals, evaluations, synthesis, directions, implications)
and assembles a single self-contained HTML report:

  • Cover page (problem, run id, date)
  • Table of contents with anchor navigation
  • Data-driven inline SVG charts (source coverage, gap significance,
    proposal verdicts, publication timeline)
  • An LLM-written executive summary tying the run together
  • Each Scribe artifact rendered as its own section, with an
    LLM-written caption / transition introducing it
  • Print CSS so the HTML can be saved to PDF from any browser

The output is one file:  artifacts/{run_id}_combined_report.html
A matching artifact row (output_type=combined_report, format=html) is
inserted so the web UI lists it alongside the other Scribe outputs.
"""

import json
import logging
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from core import database as db
from core import llm
from core.utils import generate_id

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# System prompts for the LLM narrative pieces
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """You are the Reporter agent writing the executive summary for a combined research report.

The report bundles every artifact produced by a multi-agent research pipeline. Your summary is the first thing the reader sees after the cover page.

Write 200-350 words that:
  1. State the problem the pipeline investigated.
  2. Summarize what the pipeline established — the intellectual territory, the key gaps, the viable directions.
  3. Tell the reader what the rest of this report contains and why each section matters.
  4. End with the single most important takeaway.

Tone: authoritative but accessible. A busy researcher should be able to read only this summary and still know what the run found.

Use markdown formatting (headings only sparingly — this is a flowing introduction). Output ONLY the summary text. No preamble."""

CAPTION_PROMPT = """You are the Reporter agent writing a short caption for one section of a combined research report.

You will be given:
  - The artifact's output type and title
  - Its intended audience
  - A short excerpt of its content

Write 40-80 words that:
  1. Tell the reader what this artifact is and who it is for.
  2. Explain how it relates to the other artifacts in the report (what it adds that the others do not).
  3. Flow naturally from the previous section.

Tone: helpful and concise, like a museum wall label. Output ONLY the caption text. No preamble."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(context: str, run_id: str, **kwargs) -> None:
    logger.info(f"[Reporter] Starting for run {run_id}")

    problem = ""
    if "PROBLEM:" in context:
        problem = context.split("PROBLEM:")[1].split("\n\n")[0].strip()

    # --- Gather all pipeline data -------------------------------------------
    artifacts = db.get_artifacts(run_id)
    # Exclude any previously-generated combined_report rows so a re-run
    # does not fold an old report into the new one.
    artifacts = [a for a in artifacts if a.get("output_type") != "combined_report"]

    sources      = db.fetch("sources", {"run_id": run_id})
    gaps         = db.get_gaps(run_id)
    implications = db.get_implications(run_id)
    proposals    = db.get_proposals(run_id)
    evaluations  = db.get_evaluations(run_id)
    synthesis    = db.get_synthesis(run_id)
    directions   = db.get_directions(run_id)

    # Read each artifact's file content from disk
    artifact_records = []
    for a in artifacts:
        content = ""
        path = a.get("file_path") or ""
        if path:
            try:
                p = Path(path).resolve()
                p.relative_to(ARTIFACTS_DIR.resolve())
                content = p.read_text(encoding="utf-8")
            except (ValueError, OSError) as e:
                logger.warning(f"[Reporter] Could not read {path}: {e}")
        artifact_records.append({**a, "content": content})

    # Order artifacts: understanding map first, then by output_type
    type_order = {
        "understanding_map": 0,
        "research_brief":    1,
        "blog_post":         2,
        "internal_memo":     3,
        "literature_review": 4,
        "paper_section":     5,
        "grant_background":  6,
    }
    artifact_records.sort(key=lambda a: (type_order.get(a.get("output_type"), 99),
                                         a.get("output_type", "")))

    # --- LLM: executive summary --------------------------------------------
    summary_md = ""
    try:
        summary_ctx = _build_summary_context(
            problem, run_id, sources, gaps, proposals, evaluations,
            synthesis, directions, implications, artifact_records,
        )
        summary_md = llm.call(summary_ctx, SUMMARY_PROMPT, agent_name="reporter")
        summary_md = _strip_fences(summary_md)
    except Exception as e:
        logger.warning(f"[Reporter] Executive summary LLM call failed: {e}")
        summary_md = (f"## Executive Summary\n\n"
                      f"This report bundles the outputs of a research pipeline "
                      f"investigating **{_short(problem)}**. "
                      f"See the sections below for the full artifacts.")

    # --- LLM: per-artifact captions ----------------------------------------
    captions = {}
    prev_label = "the cover page"
    for a in artifact_records:
        label = a.get("title") or a.get("output_type", "artifact")
        excerpt = (a.get("content") or "")[:600]
        try:
            cap_ctx = (
                f"ARTIFACT:\n"
                f"  output_type: {a.get('output_type')}\n"
                f"  title: {label}\n"
                f"  audience: {a.get('audience', 'researcher')}\n"
                f"  format: {a.get('format', 'md')}\n\n"
                f"PREVIOUS SECTION: {prev_label}\n\n"
                f"EXCERPT:\n{excerpt}\n"
            )
            cap = llm.call(cap_ctx, CAPTION_PROMPT, agent_name="reporter")
            captions[a.get("artifact_id")] = _strip_fences(cap).strip()
        except Exception as e:
            logger.warning(f"[Reporter] Caption LLM call failed for {label}: {e}")
            captions[a.get("artifact_id")] = ""
        prev_label = label

    # --- Assemble the HTML report ------------------------------------------
    html = _build_html(
        run_id=run_id,
        problem=problem,
        summary_md=summary_md,
        artifacts=artifact_records,
        captions=captions,
        sources=sources,
        gaps=gaps,
        proposals=proposals,
        evaluations=evaluations,
        synthesis=synthesis,
        directions=directions,
        implications=implications,
    )

    # --- Write file + persist ----------------------------------------------
    file_path = ARTIFACTS_DIR / f"{run_id}_combined_report.html"
    file_path.write_text(html, encoding="utf-8")
    logger.info(f"[Reporter] Combined report written: {file_path}")

    db.insert_artifact({
        "artifact_id":     generate_id("ART"),
        "run_id":          run_id,
        "problem_origin":  problem,
        "output_type":     "combined_report",
        "format":          "html",
        "title":           f"Combined Report: {_short(problem)}",
        "audience":        "all",
        "synthesis_id":    synthesis.get("synthesis_id", "") if synthesis else "",
        "directions_used": [d["direction_id"] for d in directions],
        "file_path":       str(file_path),
        "word_count":      len(html.split()),
        "added_by":        "Reporter",
    })

    print(f"  [Reporter] Combined report → {file_path.name}")
    print(f"  [Reporter] Bundled {len(artifact_records)} artifacts, "
          f"{len(sources)} sources, {len(gaps)} gaps, "
          f"{len(proposals)} proposals")
    logger.info(f"[Reporter] Complete — combined_report ({len(html)} chars)")


# ---------------------------------------------------------------------------
# Context for the executive summary LLM call
# ---------------------------------------------------------------------------

def _build_summary_context(problem, run_id, sources, gaps, proposals,
                           evaluations, synthesis, directions,
                           implications, artifacts) -> str:
    ctx = f"PROBLEM:\n{problem}\n"
    ctx += f"\nRUN ID: {run_id}"
    ctx += f"\nARTIFACTS IN THIS REPORT ({len(artifacts)}):"
    for a in artifacts:
        ctx += (f"\n  - {a.get('output_type')} ({a.get('format')}): "
                f"{a.get('title', '')} — {a.get('word_count', 0)} words")
    ctx += f"\n\nSOURCES: {len(sources)} total"
    by_type = {}
    for s in sources:
        by_type.setdefault(s.get("type", "unknown"), 0)
        by_type[s["type"]] += 1
    for t, n in sorted(by_type.items()):
        ctx += f"\n  - {t}: {n}"
    ctx += f"\n\nGAPS: {len(gaps)} (significance: " + ", ".join(
        f"{sig}={sum(1 for g in gaps if g.get('significance') == sig)}"
        for sig in ("High", "Medium", "Low") if any(g.get('significance') == sig for g in gaps)
    ) + ")"
    ctx += f"\n\nPROPOSALS: {len(proposals)} (statuses: " + ", ".join(
        f"{st}={sum(1 for p in proposals if p.get('status') == st)}"
        for st in ("feasible", "rejected", "deferred", "proposed")
        if any(p.get('status') == st for p in proposals)
    ) + ")"
    if synthesis:
        ctx += f"\n\nTRAJECTORY: {synthesis.get('trajectory_statement', '')}"
        ctx += f"\n\nNARRATIVE EXCERPT: {(synthesis.get('full_narrative', '') or '')[:800]}"
    if directions:
        ctx += "\n\nNEW DIRECTIONS:"
        for d in directions[:5]:
            ctx += f"\n  - [{d.get('distance_rating')}] {d.get('direction', '')[:150]}"
    if implications:
        ctx += "\n\nKEY IMPLICATIONS:"
        for i in implications[:5]:
            ctx += f"\n  - [{i.get('strength')}] {i.get('implication', '')[:150]}"
    return ctx


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def _build_html(*, run_id, problem, summary_md, artifacts, captions,
                sources, gaps, proposals, evaluations, synthesis,
                directions, implications) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    short_problem = _short(problem, 120)

    # --- Charts (inline SVG) ---
    charts_svg = _render_charts(sources, gaps, proposals, evaluations)

    # --- Summary section ---
    summary_html = md_to_html(summary_md)

    # --- Artifact sections ---
    sections_html = []
    toc_entries = []
    for idx, a in enumerate(artifacts, 1):
        anchor = f"artifact-{idx}"
        title = a.get("title") or a.get("output_type", f"Artifact {idx}")
        meta_parts = [
            a.get("output_type"),
            a.get("audience"),
            a.get("format"),
            f"{a.get('word_count', 0)} words" if a.get("word_count") else None,
        ]
        meta = " · ".join(m for m in meta_parts if m)
        caption = captions.get(a.get("artifact_id"), "")
        caption_html = md_to_html(caption) if caption else ""
        body_html = _render_artifact_body(a)
        sections_html.append(f"""
<section id="{anchor}" class="artifact-section">
  <h2>{escape(title)}</h2>
  <div class="artifact-meta">{escape(meta)}</div>
  {caption_html and f'<div class="caption">{caption_html}</div>' or ''}
  <div class="artifact-body">{body_html}</div>
</section>""")
        toc_entries.append(
            f'<li><a href="#{anchor}">{escape(title)}</a> '
            f'<span class="toc-meta">{escape(a.get("output_type", ""))}</span></li>'
        )

    toc_html = "\n".join(toc_entries)

    # --- Data appendix (compact tables) ---
    appendix_html = _render_appendix(
        sources, gaps, proposals, evaluations, directions, implications,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Combined Report — {escape(short_problem)}</title>
<style>
{_STYLES}
</style>
</head>
<body>

<!-- ============================ COVER ============================ -->
<section class="cover">
  <div class="cover-inner">
    <div class="cover-eyebrow">Research Pipeline Report</div>
    <h1 class="cover-title">{escape(short_problem)}</h1>
    <div class="cover-meta">
      <div><span class="label">Run</span> <code>{escape(run_id)}</code></div>
      <div><span class="label">Generated</span> {escape(now)}</div>
      <div><span class="label">Artifacts</span> {len(artifacts)}</div>
      <div><span class="label">Sources</span> {len(sources)}</div>
    </div>
    <div class="cover-print-hint">Tip: use your browser's Print dialog (Ctrl/Cmd+P) to save this report as PDF.</div>
  </div>
</section>

<!-- ============================ TOC ============================ -->
<nav class="toc" id="toc">
  <h2>Contents</h2>
  <ol>
    <li><a href="#summary">Executive Summary</a></li>
    <li><a href="#charts">Pipeline at a Glance</a></li>
    {toc_html and ''.join(f'<li><a href="#artifact-{i}">{escape(a.get("title") or a.get("output_type",""))}</a> <span class="toc-meta">{escape(a.get("output_type",""))}</span></li>' for i, a in enumerate(artifacts, 1)) or ''}
    <li><a href="#appendix">Data Appendix</a></li>
  </ol>
</nav>

<!-- ============================ SUMMARY ============================ -->
<section id="summary" class="report-section">
  <h2>Executive Summary</h2>
  {summary_html}
</section>

<!-- ============================ CHARTS ============================ -->
<section id="charts" class="report-section">
  <h2>Pipeline at a Glance</h2>
  <p class="section-intro">Data-driven view of what the pipeline gathered and concluded.</p>
  <div class="charts-grid">
    {charts_svg}
  </div>
</section>

<!-- ============================ ARTIFACTS ============================ -->
{''.join(sections_html)}

<!-- ============================ APPENDIX ============================ -->
<section id="appendix" class="report-section appendix">
  <h2>Data Appendix</h2>
  <p class="section-intro">Raw pipeline outputs behind the charts and narratives above.</p>
  {appendix_html}
</section>

<footer class="report-footer">
  <p>Generated by the Reporter agent · Run <code>{escape(run_id)}</code> · {escape(now)}</p>
</footer>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Artifact body rendering
# ---------------------------------------------------------------------------

def _render_artifact_body(a: dict) -> str:
    fmt = a.get("format", "md")
    content = a.get("content") or "(the file is missing on disk)"
    if fmt == "md":
        return md_to_html(content)
    elif fmt == "tex":
        # Render LaTeX as a scrollable code block
        return f'<pre class="latex-block"><code>{escape(content)}</code></pre>'
    else:
        return f'<pre class="code-block"><code>{escape(content)}</code></pre>'


# ---------------------------------------------------------------------------
# Charts — pure inline SVG, no external deps
# ---------------------------------------------------------------------------

def _render_charts(sources, gaps, proposals, evaluations) -> str:
    charts = []
    # 1. Source coverage by type
    by_type = {}
    for s in sources:
        t = s.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    if by_type:
        charts.append(_bar_chart(
            "Sources by Type",
            [(k, v) for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
            color="#4f7cff",
        ))
    # 2. Gap significance
    sig_counts = {}
    for g in gaps:
        sig = g.get("significance", "Unknown")
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
    if sig_counts:
        order = ["High", "Medium", "Low", "Unknown"]
        data = [(k, sig_counts.get(k, 0)) for k in order if sig_counts.get(k)]
        charts.append(_bar_chart("Gaps by Significance", data, color="#e85d4a"))
    # 3. Proposal verdicts
    verdict_counts = {}
    for e in evaluations:
        v = e.get("verdict", "unknown")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    if not verdict_counts and proposals:
        # Fall back to proposal statuses
        for p in proposals:
            st = p.get("status", "proposed")
            verdict_counts[st] = verdict_counts.get(st, 0) + 1
    if verdict_counts:
        charts.append(_bar_chart(
            "Proposal Verdicts",
            [(k, v) for k, v in sorted(verdict_counts.items(), key=lambda x: -x[1])],
            color="#3aa856",
        ))
    # 4. Publication timeline
    year_counts = {}
    for s in sources:
        y = s.get("year")
        if y and isinstance(y, int) and 1900 <= y <= 2100:
            year_counts[y] = year_counts.get(y, 0) + 1
    if year_counts:
        # Bucket into decades if range is wide
        years = sorted(year_counts)
        span = years[-1] - years[0]
        if span > 30:
            buckets = {}
            for y, n in year_counts.items():
                decade = (y // 10) * 10
                buckets[decade] = buckets.get(decade, 0) + n
            data = [(f"{d}s", n) for d, n in sorted(buckets.items())]
        else:
            data = [(str(y), year_counts[y]) for y in years]
        charts.append(_bar_chart("Sources by Year", data, color="#9b59b6"))

    if not charts:
        return '<p class="muted">No chartable data in this run.</p>'
    return "\n".join(charts)


def _bar_chart(title: str, data: list, color: str = "#4f7cff") -> str:
    """Render a horizontal bar chart as inline SVG."""
    if not data:
        return ""
    max_val = max(v for _, v in data) or 1
    bar_h = 26
    gap = 8
    label_w = 130
    chart_w = 260
    h = len(data) * (bar_h + gap) + 40
    total_w = label_w + chart_w + 60
    rows = []
    for i, (label, val) in enumerate(data):
        y = 30 + i * (bar_h + gap)
        bar_len = int((val / max_val) * chart_w)
        rows.append(f"""
    <text x="{label_w - 8}" y="{y + bar_h - 7}" text-anchor="end"
          class="chart-label">{escape(str(label))}</text>
    <rect x="{label_w}" y="{y}" width="{bar_len}" height="{bar_h}"
          rx="3" fill="{color}" opacity="0.85"/>
    <text x="{label_w + bar_len + 6}" y="{y + bar_h - 7}"
          class="chart-value">{val}</text>""")
    rows_svg = "".join(rows)
    return f"""
<figure class="chart-card">
  <figcaption>{escape(title)}</figcaption>
  <svg viewBox="0 0 {total_w} {h}" xmlns="http://www.w3.org/2000/svg"
       role="img" aria-label="{escape(title)}" class="chart-svg">
    {rows_svg}
  </svg>
</figure>"""


# ---------------------------------------------------------------------------
# Data appendix
# ---------------------------------------------------------------------------

def _render_appendix(sources, gaps, proposals, evaluations, directions,
                     implications) -> str:
    parts = []

    # Sources table
    if sources:
        rows = []
        for s in sources[:50]:
            authors = _parse_authors_short(s.get("authors"))
            rows.append(
                f"<tr><td>{escape(str(s.get('year','')))}</td>"
                f"<td>{escape(authors)}</td>"
                f"<td>{escape((s.get('title') or '')[:80])}</td>"
                f"<td><span class='tag'>{escape(s.get('type',''))}</span></td></tr>"
            )
        parts.append(f"""
<h3>Sources ({len(sources)})</h3>
<table class="data-table"><thead><tr>
<th>Year</th><th>Authors</th><th>Title</th><th>Type</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>""")

    # Gaps
    if gaps:
        rows = []
        for g in gaps[:30]:
            rows.append(
                f"<tr><td><span class='tag sig-{escape((g.get('significance') or '').lower())}'>"
                f"{escape(g.get('significance',''))}</span></td>"
                f"<td>{escape(g.get('gap_type',''))}</td>"
                f"<td>{escape((g.get('description') or '')[:120])}</td></tr>"
            )
        parts.append(f"""
<h3>Gaps ({len(gaps)})</h3>
<table class="data-table"><thead><tr>
<th>Significance</th><th>Type</th><th>Description</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>""")

    # Proposals + verdicts
    if proposals:
        rows = []
        for p in proposals[:30]:
            ev = next((e for e in evaluations
                       if e.get("proposal_id") == p.get("proposal_id")), {})
            verdict = ev.get("verdict", p.get("status", ""))
            rows.append(
                f"<tr><td><span class='tag verdict-{escape((verdict or '').lower().replace(' ','-'))}'>"
                f"{escape(verdict)}</span></td>"
                f"<td>{escape(p.get('proposal_type',''))}</td>"
                f"<td>{escape((p.get('proposal') or '')[:120])}</td></tr>"
            )
        parts.append(f"""
<h3>Proposals ({len(proposals)})</h3>
<table class="data-table"><thead><tr>
<th>Verdict</th><th>Type</th><th>Proposal</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>""")

    # Directions
    if directions:
        rows = []
        for d in directions[:20]:
            rows.append(
                f"<tr><td>{escape(d.get('distance_rating',''))}</td>"
                f"<td>{escape(d.get('direction_type',''))}</td>"
                f"<td>{escape((d.get('direction') or '')[:120])}</td></tr>"
            )
        parts.append(f"""
<h3>New Directions ({len(directions)})</h3>
<table class="data-table"><thead><tr>
<th>Distance</th><th>Type</th><th>Direction</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>""")

    # Implications
    if implications:
        rows = []
        for i in implications[:20]:
            rows.append(
                f"<tr><td>{escape(i.get('strength',''))}</td>"
                f"<td>{escape(i.get('implication_type',''))}</td>"
                f"<td>{escape((i.get('implication') or '')[:120])}</td></tr>"
            )
        parts.append(f"""
<h3>Implications ({len(implications)})</h3>
<table class="data-table"><thead><tr>
<th>Strength</th><th>Type</th><th>Implication</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>""")

    return "\n".join(parts) if parts else "<p class='muted'>No appendix data.</p>"


# ---------------------------------------------------------------------------
# Lightweight Markdown → HTML (subset, safe)
# ---------------------------------------------------------------------------

def md_to_html(md: str) -> str:
    """Convert a subset of markdown to safe HTML for embedding in the report."""
    if not md:
        return ""
    lines = md.split("\n")
    out = []
    in_list = False
    in_ol = False
    in_code = False
    code_lang = ""
    code_buf = []

    def flush_list():
        nonlocal in_list, in_ol
        if in_list:
            out.append("</ul>")
            in_list = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for line in lines:
        # Code fence
        if line.strip().startswith("```"):
            if in_code:
                out.append(f'<pre class="code-block"><code>'
                           f'{escape(chr(10).join(code_buf))}</code></pre>')
                code_buf = []
                in_code = False
            else:
                flush_list()
                in_code = True
                code_lang = line.strip()[3:].strip()
            continue
        if in_code:
            code_buf.append(line)
            continue

        stripped = line.strip()

        # Headings
        if stripped.startswith("###### "):
            flush_list()
            out.append(f"<h6>{_inline(stripped[6:])}</h6>")
        elif stripped.startswith("##### "):
            flush_list()
            out.append(f"<h5>{_inline(stripped[5:])}</h5>")
        elif stripped.startswith("#### "):
            flush_list()
            out.append(f"<h4>{_inline(stripped[4:])}</h4>")
        elif stripped.startswith("### "):
            flush_list()
            out.append(f"<h3>{_inline(stripped[3:])}</h3>")
        elif stripped.startswith("## "):
            flush_list()
            out.append(f"<h3>{_inline(stripped[2:])}</h3>")
        elif stripped.startswith("# "):
            flush_list()
            out.append(f"<h2>{_inline(stripped[1:])}</h2>")
        elif stripped.startswith("---") and len(stripped) >= 3:
            flush_list()
            out.append("<hr>")
        elif re.match(r"^\d+\.\s+", stripped):
            if in_list:
                flush_list()
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            item = re.sub(r"^\d+\.\s+", "", stripped)
            out.append(f"<li>{_inline(item)}</li>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if in_ol:
                flush_list()
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = stripped[2:]
            out.append(f"<li>{_inline(item)}</li>")
        elif stripped.startswith("> "):
            flush_list()
            out.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif stripped == "":
            flush_list()
        else:
            flush_list()
            out.append(f"<p>{_inline(stripped)}</p>")

    if in_code:
        out.append(f'<pre class="code-block"><code>'
                   f'{escape(chr(10).join(code_buf))}</code></pre>')
    flush_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Inline markdown: bold, italic, code, links. Escapes HTML first."""
    text = escape(text)
    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                  r'<a href="\2">\1</a>', text)
    # Bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


# ---------------------------------------------------------------------------
# Styles (print-friendly)
# ---------------------------------------------------------------------------

_STYLES = r"""
:root {
  --ink: #1a1a2e;
  --muted: #6b7280;
  --accent: #4f7cff;
  --bg: #ffffff;
  --card-bg: #f8f9fb;
  --border: #e5e7eb;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: var(--ink);
  background: var(--bg);
  margin: 0;
  line-height: 1.6;
  font-size: 15px;
}

/* Cover */
.cover {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1a1a2e 0%, #2d3561 50%, #4f7cff 100%);
  color: #fff;
  page-break-after: always;
}
.cover-inner { text-align: center; padding: 2rem; max-width: 700px; }
.cover-eyebrow {
  text-transform: uppercase; letter-spacing: 3px; font-size: 13px;
  opacity: 0.8; margin-bottom: 1rem;
}
.cover-title {
  font-size: 2.4rem; font-weight: 700; line-height: 1.2;
  margin: 0 0 2rem 0;
}
.cover-meta {
  display: flex; flex-wrap: wrap; justify-content: center; gap: 1.5rem;
  margin-bottom: 2rem; font-size: 14px;
}
.cover-meta .label { opacity: 0.6; margin-right: 4px; }
.cover-meta code { background: rgba(255,255,255,0.15); padding: 2px 6px; border-radius: 3px; }
.cover-print-hint { font-size: 12px; opacity: 0.6; }

/* TOC */
.toc {
  max-width: 800px; margin: 0 auto; padding: 3rem 2rem;
  page-break-after: always;
}
.toc h2 { border-bottom: 2px solid var(--accent); padding-bottom: .5rem; }
.toc ol { list-style: decimal; padding-left: 1.5rem; }
.toc li { margin: .4rem 0; }
.toc a { color: var(--accent); text-decoration: none; }
.toc a:hover { text-decoration: underline; }
.toc-meta { color: var(--muted); font-size: 12px; }

/* Sections */
.report-section, .artifact-section {
  max-width: 800px; margin: 0 auto; padding: 2rem;
  page-break-inside: auto;
}
.report-section h2, .artifact-section h2 {
  border-bottom: 2px solid var(--accent); padding-bottom: .4rem;
  margin-top: 2rem;
}
.report-section h3, .artifact-section h3 { margin-top: 1.5rem; }
.section-intro { color: var(--muted); font-style: italic; }
.artifact-meta {
  font-size: 13px; color: var(--muted); margin: -.5rem 0 1rem 0;
}
.caption {
  background: var(--card-bg); border-left: 3px solid var(--accent);
  padding: .75rem 1rem; margin: 1rem 0; font-size: 14px;
  border-radius: 0 4px 4px 0;
}
.artifact-body { margin-top: 1rem; }
.artifact-body h2, .artifact-body h3 { border: none; padding: 0; }
.artifact-body pre { overflow-x: auto; }

/* Charts */
.charts-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 1.5rem; margin-top: 1.5rem;
}
.chart-card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 8px; padding: 1rem; margin: 0;
}
.chart-card figcaption {
  font-weight: 600; margin-bottom: .5rem; font-size: 14px;
}
.chart-svg { width: 100%; height: auto; }
.chart-label { font-size: 11px; fill: var(--ink); }
.chart-value { font-size: 11px; fill: var(--muted); font-weight: 600; }

/* Tables */
.data-table {
  width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 13px;
}
.data-table th, .data-table td {
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.data-table th { background: var(--card-bg); font-weight: 600; }
.data-table tr:nth-child(even) td { background: #fafbfc; }

/* Tags */
.tag {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 11px; font-weight: 600; background: #eef; color: #4f7cff;
}
.tag.sig-high { background: #fee; color: #c33; }
.tag.sig-medium { background: #ffe; color: #a80; }
.tag.sig-low { background: #eef; color: #669; }
.tag.verdict-feasible { background: #e6f7ec; color: #2a8; }
.tag.verdict-partially-feasible { background: #fff7e6; color: #a80; }
.tag.verdict-unfeasible { background: #fee; color: #c33; }
.tag.verdict-insufficient-evidence { background: #f0f0f0; color: #888; }
.tag.verdict-rejected { background: #fee; color: #c33; }
.tag.verdict-deferred { background: #fff7e6; color: #a80; }
.tag.verdict-proposed { background: #eef; color: #4f7cff; }

/* Code blocks */
.code-block, .latex-block {
  background: #1e1e2e; color: #e0e0e0; padding: 1rem;
  border-radius: 6px; overflow-x: auto; font-size: 13px;
  font-family: "SF Mono", Monaco, Consolas, monospace; line-height: 1.5;
}
.latex-block { white-space: pre-wrap; word-break: break-word; }

/* Footer */
.report-footer {
  text-align: center; padding: 2rem; color: var(--muted);
  font-size: 12px; border-top: 1px solid var(--border); margin-top: 2rem;
}
.report-footer code { background: var(--card-bg); padding: 1px 4px; border-radius: 2px; }

.muted { color: var(--muted); }

/* Blockquotes */
blockquote {
  border-left: 3px solid var(--border); margin: 1rem 0; padding: .5rem 1rem;
  color: var(--muted); background: var(--card-bg); border-radius: 0 4px 4px 0;
}

/* Print */
@media print {
  .cover { min-height: auto; page-break-after: always; }
  .toc { page-break-after: always; }
  .report-section, .artifact-section {
    page-break-inside: avoid; padding: 1rem 0;
  }
  .chart-card, table { page-break-inside: avoid; }
  a { color: var(--ink); text-decoration: none; }
  .toc a { color: var(--ink); }
  .chart-card { border: 1px solid #ccc; }
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short(text: str, n: int = 60) -> str:
    text = (text or "").strip()
    return text[:n] + ("..." if len(text) > n else "")


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:markdown|md|html)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_authors_short(raw) -> str:
    if not raw:
        return ""
    try:
        authors = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else [raw]
    except (json.JSONDecodeError, TypeError):
        authors = [str(raw)]
    if not authors:
        return ""
    if len(authors) <= 2:
        return ", ".join(authors)
    return f"{authors[0]} et al."
