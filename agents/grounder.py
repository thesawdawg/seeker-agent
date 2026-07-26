"""
Grounder Agent
--------------
Excavates the intellectual origins of the research problem.

Pipeline:
  1. DECOMPOSE  — LLM breaks problem into complete sub-question tree
  2. QUERY GEN  — each sub-question → 2-3 contextual search queries
  3. SEARCH     — OpenAlex, arXiv, Semantic Scholar, Google Books, Open Library,
                  web search (Claude native) for each query
  4. SYNTHESIZE — LLM maps intellectual genealogy from all gathered sources
  5. SAVE       — seminal works → DB, foundations doc → artifacts/

Books are first-class sources here alongside papers.
Web search covers breadth that academic APIs miss.
"""

import re
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import database as db
from core import llm
from core import progress
from core.utils import generate_id, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1 — Problem decomposition prompt
# ---------------------------------------------------------------------------

DECOMPOSE_SYSTEM = """You are a research decomposition specialist.

Given a research problem, decompose it into the most complete and exhaustive tree of sub-questions needed to fully understand and answer it. 

Rules:
- Work from fundamentals upward — start with definitional questions, then structural, then relational, then positional
- Every concept that appears in the problem must be unpacked
- Ask questions that a researcher would need to answer BEFORE addressing the main problem
- Include both empirical questions ("what is X?") and conceptual questions ("what does X mean?")
- Typical depth: 6-12 sub-questions for a philosophical/interdisciplinary problem

Example: "What is the place of AI in human life?"
→ What is intelligence? What forms does intelligence take? What are the defining characteristics of human intelligence? What distinguishes human intelligence from other forms? What is artificial intelligence? What are AI's core characteristics and limitations? How does AI processing differ structurally from human cognition? What does "place" mean — functional role, ontological status, normative position? How have technologies previously been positioned relative to human life? What is the relationship between a tool and the being that uses it? How should AI be positioned in human life given the above?

Output ONLY valid JSON:
{
  "sub_questions": [
    {
      "id": "Q1",
      "question": "full question text",
      "level": "foundational|structural|relational|positional",
      "rationale": "why this sub-question must be answered"
    }
  ],
  "decomposition_logic": "one paragraph explaining the decomposition strategy"
}"""


# ---------------------------------------------------------------------------
# Step 2 — Query generation prompt
# ---------------------------------------------------------------------------

QUERY_GEN_SYSTEM = """You are a research query specialist.

Given a sub-question and its context, generate targeted search queries for academic databases and book catalogs.

Rules for queries:
- NEVER use single words alone — always combine keyword + 1-2 word context
- Academic queries: combine the core concept with its disciplinary context
  BAD: "intelligence"
  GOOD: "human intelligence definition", "intelligence forms cognitive science", "intelligence measurement history"
- Book queries: use author names + concept, or classic title keywords
  GOOD: "Turing computing machinery intelligence", "Dreyfus artificial intelligence critique", "intelligence philosophy mind"
- Generate exactly 3 queries: one for academic papers, one for books, one broader/web

Output ONLY valid JSON:
{
  "paper_query": "2-4 word academic query",
  "book_query": "2-4 word book/monograph query",
  "web_query": "3-5 word broader search query"
}"""


# ---------------------------------------------------------------------------
# Step 3 — Synthesis prompt
# ---------------------------------------------------------------------------

SYNTHESIS_SYSTEM = """You are the Grounder agent in a multi-agent research pipeline.

Your role is to excavate the intellectual origins of the research problem using the gathered sources.

You have been given:
- The decomposed sub-questions
- Search results from academic databases, book catalogs, and web search

From this material, synthesize the intellectual foundations:
1. Extract all core themes embedded in the problem
2. For each theme, identify the oldest, most influential foundational works from the results
3. Find where themes intersected and produced foundational questions
4. Extract original definitions — how key concepts were first defined and by whom
5. Establish the fundamental whys — what original motivations gave birth to this problem
6. Map the intellectual genealogy — who built on whom

Search backward in time — prioritize oldest cited works.
Do NOT analyze current state, identify gaps, or propose solutions.
Include BOOKS alongside papers — foundational books matter as much as articles.

Output ONLY valid JSON:
{
  "themes_extracted": [
    {"theme": "name", "description": "why relevant to problem"}
  ],
  "seminal_works": [
    {
      "title": "full title",
      "authors": ["Author Name"],
      "year": 1950,
      "source": "source name",
      "material_type": "paper|book|chapter",
      "doi": "",
      "isbn": "",
      "abstract": "brief description of what it established",
      "active_link": "url if known",
      "seminal_reason": "one line — what it established and why foundational",
      "intersection_tags": ["theme1 x theme2"],
      "theme_tags": ["theme1"]
    }
  ],
  "intellectual_genealogy": "narrative of who built on whom — at least 3 paragraphs",
  "fundamental_whys": "original motivations behind this problem — at least 2 paragraphs",
  "original_definitions": [
    {"concept": "name", "definition": "text", "defined_by": "who", "year": 0}
  ],
  "intersection_points": [
    {"themes": ["t1", "t2"], "description": "how they met and what question emerged"}
  ],
  "proposed_new_themes": [
    {
      "theme_id": "snake_case_id",
      "label": "Human readable label",
      "reason": "why relevant but missing from config",
      "suggested_keywords": [
        {"seed": "keyword", "expansion_depth": 1, "boundary_note": "stay within..."}
      ],
      "suggested_sources": ["openalex"]
    }
  ],
  "assumptions_flagged": [
    {"assumption": "text", "note": "why disputed or unclear"}
  ]
}"""


# ---------------------------------------------------------------------------
# Source handlers — books + papers + web
#
# The academic and book source handlers (OpenAlex, Semantic Scholar, Consensus,
# Google Books, Open Library) were consolidated onto the shared
# agents.social.SOURCE_HANDLERS registry in review O1, so this module no
# longer keeps its own copies. Routing through the shared registry gives
# Grounder's calls the rate limiter, retry/backoff, circuit breaker, and
# global daily-limit tracking that the Social agent already had.
#
# _search_web is kept here because it uses Anthropic's server-side web_search
# tool, which has no OpenAI-compatible equivalent and so does not fit the
# shared SourceHandler base class. It still routes through the rate limiter
# for the "web_search" source so its calls are coordinated and visible.
# ---------------------------------------------------------------------------

def _search_web(query: str) -> list[dict]:
    """
    Broader coverage via Anthropic's server-side web_search tool.

    This is a search source, not a reasoning call, and the tool has no
    OpenAI-compatible equivalent — so it stays Anthropic-specific rather than
    going through the provider chain. It reads the 'anthropic' provider from
    config.json and skips silently when that provider is not configured, which
    is the normal case for a local-only deployment.
    """
    import requests
    from core import llm

    provider = llm.get_client().settings.providers.get("anthropic")
    if not provider or not provider.configured or not provider.api_key:
        logger.info("[Grounder/WebSearch] anthropic provider not configured — skipping web search")
        return []

    model = provider.model_for_role("light") or provider.model_for_role("primary")
    if not model:
        logger.info("[Grounder/WebSearch] no anthropic model configured — skipping web search")
        return []

    try:
        response = requests.post(
            f"{provider.base_url}/v1/messages",
            headers={
                "x-api-key": provider.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Search for foundational academic sources on: {query}\n"
                        f"List the most important books, papers, and authors. "
                        f"Include publication years and authors where known."
                    )
                }],
            },
            timeout=120,
        )
        response.raise_for_status()
        blocks = response.json().get("content") or []
        full_text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not full_text:
            return []
        # Return as a single web-search result entry for the synthesis prompt
        return [{"title": f"Web search: {query}", "source": "web_search",
                 "material_type": "web", "abstract": full_text[:1500],
                 "authors": [], "year": None, "doi": "", "active_link": ""}]
    except Exception as e:
        logger.warning(f"[Grounder/WebSearch] {e}")
        return []


# ---------------------------------------------------------------------------
# Link verification
# ---------------------------------------------------------------------------

def _verify_link(url: str) -> str:
    """Delegate to the shared SourceHandler._check_link (review O8)."""
    from agents.social import SourceHandler
    return SourceHandler()._check_link(url)


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(context: str, run_id: str, **kwargs):
    logger.info(f"[Grounder] Starting for run {run_id}")

    problem = ""
    if "PROBLEM:" in context:
        problem = context.split("PROBLEM:")[1].split("\n\n")[0].strip()

    # Load agent source config
    _config = load_config()
    _allowed = set(_config.get("agent_sources", {}).get("grounder",
        ["openalex", "semantic_scholar", "consensus",
         "google_books", "open_library", "web"]))
    # Per-source result limits — configurable so a researcher can choose
    # shallow vs deep scans without editing code (review U5).
    _limits = _config.get("agent_sources", {}).get("grounder_limits", {})
    def _lim(name: str, default: int) -> int:
        return int(_limits.get(name, default))
    def _src_on(name: str) -> bool:
        return name in _allowed

    # Initialize argument tree
    from core.argument_tree import TreeBuilder
    tree = TreeBuilder(run_id)
    root_id = tree.create_root(problem)
    print(f"  [Grounder] Tree root created: {root_id}")

    # -----------------------------------------------------------------------
    # Step 1 — Decompose problem into sub-questions
    # -----------------------------------------------------------------------
    print("  [Grounder] Step 1 — decomposing problem into sub-questions...")
    try:
        decomp_resp = llm.call(
            f"Research problem to decompose:\n\n{problem}",
            DECOMPOSE_SYSTEM, agent_name="grounder"
        )
        decomp_clean = re.sub(r"```(?:json)?|```", "", decomp_resp).strip()
        decomp_data  = json.loads(decomp_clean)
    except Exception as e:
        logger.warning(f"[Grounder] Decomposition failed: {e} — using problem as single question")
        decomp_data = {
            "sub_questions": [{"id": "Q1", "question": problem,
                                "level": "positional", "rationale": "original problem"}],
            "decomposition_logic": "Decomposition failed — using original problem"
        }

    sub_questions = decomp_data.get("sub_questions", [])
    print(f"  [Grounder] {len(sub_questions)} sub-questions generated")

    # Add sub-questions as tree nodes
    q_node_map: dict[str, str] = {}  # maps Q1, Q2... → tree node_id
    for sq in sub_questions:
        q_id = sq.get("id", "?")
        node_id = tree.add_question(
            root_id, sq.get("question", ""),
            question_level=sq.get("level", "foundational"),
            agent="grounder",
        )
        q_node_map[q_id] = node_id
        print(f"    {q_id} [{sq.get('level','')}] {sq.get('question','')[:60]}")

    # -----------------------------------------------------------------------
    # Step 2 — Generate queries per sub-question and search all sources
    #          For each result, save to DB AND add to tree
    # -----------------------------------------------------------------------
    all_sources: list[dict] = []
    source_texts: list[str] = []

    for sq in sub_questions:
        q_text = sq.get("question", "")
        q_id   = sq.get("id", "?")
        q_node = q_node_map.get(q_id, root_id)
        print(f"  [Grounder] Querying sources for {q_id}: {q_text[:60]}...")

        # Generate contextual queries
        try:
            qgen_resp = llm.call(
                f"Sub-question: {q_text}\nProblem context: {problem}",
                QUERY_GEN_SYSTEM, agent_name="social"
            )
            qgen_clean = re.sub(r"```(?:json)?|```", "", qgen_resp).strip()
            queries    = json.loads(qgen_clean)
        except Exception as e:
            logger.warning(f"[Grounder] Query gen failed for {q_id}: {e}")
            words = [w for w in q_text.lower().split()
                     if len(w) > 4 and w not in {"what","does","have","that","this","with","from","into"}]
            queries = {
                "paper_query": " ".join(words[:3]),
                "book_query":  " ".join(words[:2]) + " history",
                "web_query":   " ".join(words[:4])
            }

        paper_query = queries.get("paper_query", "")
        book_query  = queries.get("book_query", "")
        web_query   = queries.get("web_query", "")

        print(f"    Papers: '{paper_query}' | Books: '{book_query}' | Web: '{web_query}'")

        def _process_results(results: list[dict], source_label: str,
                             evidence_type: str = "paper"):
            """Save each result to all_sources and build tree nodes."""
            for r in results:
                all_sources.append(r)
                title = r.get('title', '')
                authors = r.get('authors', [])
                year = r.get('year', '?')
                abstract = r.get('abstract', '')[:300]
                author_str = ', '.join(authors[:2])

                source_texts.append(
                    f"[{q_id}/{source_label}] {title} ({year}) "
                    f"— {author_str} | {abstract}"
                )

                # Generate a temporary source_id for tree linkage
                # (will be replaced with real DB source_id after synthesis)
                temp_src_id = generate_id("TSRC")
                r['_temp_src_id'] = temp_src_id
                r['_question_id'] = q_id
                r['_evidence_type'] = evidence_type

                # Add claim + evidence to tree under this question
                claim_text = (
                    f"{title} ({author_str}, {year}): "
                    f"{abstract[:150]}"
                )
                claim_id = tree.add_claim(
                    q_node, claim_text,
                    confidence=0.5,  # initial — refined in synthesis
                    source_ids=[temp_src_id],
                    agent="grounder",
                )
                tree.add_evidence(
                    claim_id, temp_src_id,
                    evidence_type=evidence_type,
                    relationship="supports",
                    snippet=abstract[:300],
                    agent="grounder",
                    metadata={
                        "title": title,
                        "authors": authors[:3],
                        "year": year,
                        "source_name": r.get('source_name', source_label.lower()),
                    },
                )

        # Academic papers + books — routed through the shared SOURCE_HANDLERS
        # registry in social.py so every source gets the rate limiter, the
        # retry/backoff machinery, the circuit breaker, and the global daily
        # limit (review O1 / R1 / E6). Web search stays Anthropic-specific
        # (no OpenAI-compatible equivalent) but still goes through the limiter.
        from agents.social import SOURCE_HANDLERS
        from core.rate_limiter import get_limiter, SourceUnavailable

        def _run_shared_source(source_id: str, label: str, query: str,
                               limit: int, evidence_type: str):
            """Call a shared SourceHandler and record health + tree nodes."""
            handler = SOURCE_HANDLERS.get(source_id)
            if not handler:
                return
            progress.note(source_id, "searching", query)
            try:
                results = handler.search(query, [], limit, run_id=run_id)
                db.record_source_health(
                    run_id, source_id, "grounder",
                    status="ok" if results else "degraded",
                    results_returned=len(results), calls_made=1,
                )
            except SourceUnavailable:
                results = []
                db.record_source_health(
                    run_id, source_id, "grounder",
                    status="skipped", last_error="circuit breaker tripped",
                )
            except Exception as e:
                logger.warning(f"[Grounder/{source_id}] {e}")
                results = []
                db.record_source_health(
                    run_id, source_id, "grounder",
                    status="failed", last_error=str(e)[:200],
                )
            _process_results(results, label, evidence_type)

        if paper_query and _src_on("openalex"):
            _run_shared_source("openalex", "OpenAlex", paper_query, _lim("openalex", 4), "paper")
        if paper_query and _src_on("semantic_scholar"):
            _run_shared_source("semantic_scholar", "S2", paper_query, _lim("semantic_scholar", 3), "paper")
        if paper_query and _src_on("consensus"):
            _run_shared_source("consensus", "Consensus", paper_query, _lim("consensus", 10), "paper")
        if book_query and _src_on("google_books"):
            _run_shared_source("google_books", "GoogleBooks", book_query, _lim("google_books", 3), "book")
        if book_query and _src_on("open_library"):
            _run_shared_source("open_library", "OpenLibrary", book_query, _lim("open_library", 3), "book")

        # Web search — broader coverage. Anthropic server-side web_search tool,
        # no OpenAI-compatible equivalent, so it stays Anthropic-specific. It
        # still routes through the rate limiter so its calls are visible in
        # the run summary and coordinated with other sources.
        if web_query and _src_on("web"):
            progress.note("web_search", "searching", web_query)
            limiter = get_limiter(run_id)
            try:
                ok = limiter.wait("web_search")
            except SourceUnavailable:
                ok = False
            if ok:
                try:
                    results = _search_web(web_query)
                    limiter.record_success("web_search")
                    db.record_source_health(
                        run_id, "web_search", "grounder",
                        status="ok" if results else "degraded",
                        results_returned=len(results), calls_made=1,
                    )
                except Exception as e:
                    logger.warning(f"[Grounder/WebSearch] {e}")
                    limiter.record_failure("web_search")
                    results = []
                    db.record_source_health(
                        run_id, "web_search", "grounder",
                        status="failed", last_error=str(e)[:200],
                    )
            else:
                results = []
                db.record_source_health(
                    run_id, "web_search", "grounder",
                    status="skipped", last_error="rate-limited",
                )
            _process_results(results, "WebSearch", "other")

    print(f"  [Grounder] {len(all_sources)} total sources gathered across {len(sub_questions)} sub-questions")
    tree_stats = tree.get_stats()
    print(f"  [Grounder] Tree: {tree_stats['total_nodes']} nodes, {tree_stats['unique_sources']} sources")

    # -----------------------------------------------------------------------
    # Step 3 — LLM synthesis from all gathered material
    # -----------------------------------------------------------------------
    print("  [Grounder] Step 3 — synthesizing intellectual foundations...")

    synthesis_prompt = f"""{context}

---
PROBLEM DECOMPOSITION:
{decomp_data.get('decomposition_logic','')}

Sub-questions explored:
{chr(10).join(f"  {sq['id']}: {sq['question']}" for sq in sub_questions)}

---
GATHERED SOURCES ({len(all_sources)} total):

{chr(10).join(source_texts[:80])}

---
Using the above sources, synthesize the intellectual foundations of the problem.
Prioritize genuinely old, foundational works. Include books alongside papers.
For each seminal work, use the exact title and author from the sources above where possible."""

    try:
        response = llm.call(synthesis_prompt, SYNTHESIS_SYSTEM, agent_name="grounder")
    except Exception as e:
        logger.error(f"[Grounder] Synthesis LLM call failed: {e}")
        raise

    try:
        clean = re.sub(r"```(?:json)?|```", "", response).strip()
        data  = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end   = clean.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(clean[start:end+1])
            except Exception:
                data = {}
        else:
            data = {}
        if not data:
            logger.warning("[Grounder] Synthesis JSON parse failed — partial result")
            from core import progress
            progress.warn("Synthesis JSON parse failed — seminal works and themes may be incomplete. The raw LLM response was kept in intellectual_genealogy.")
            data = {
                "themes_extracted": [], "seminal_works": [],
                "intellectual_genealogy": response[:3000],
                "fundamental_whys": "", "original_definitions": [],
                "intersection_points": [], "proposed_new_themes": [],
                "assumptions_flagged": []
            }

    # -----------------------------------------------------------------------
    # Step 4 — Save seminal works to database + update tree with real source_ids
    # -----------------------------------------------------------------------
    saved = 0
    for work in data.get("seminal_works", []):
        if not work.get("title"):
            continue
        link_status = _verify_link(work.get("active_link", ""))
        source_id = generate_id("SEM")
        ok = db.upsert_source({
            "source_id":         source_id,
            "title":             work.get("title", ""),
            "authors":           work.get("authors", []),
            "year":              work.get("year"),
            "source_name":       work.get("source", "grounder"),
            "doi":               work.get("doi", ""),
            "abstract":          work.get("abstract", ""),
            "active_link":       work.get("active_link", ""),
            "theme_tags":        work.get("theme_tags", []),
            "type":              "seminal",
            "seminal_reason":    work.get("seminal_reason", ""),
            "intersection_tags": work.get("intersection_tags", []),
            "added_by":          "Grounder",
            "date_collected":    datetime.now(timezone.utc).isoformat(),
            "last_checked":      datetime.now(timezone.utc).isoformat(),
            "link_status":       link_status,
            "run_id":            run_id,
        })
        if ok:
            saved += 1
        time.sleep(0.05)

    # Save proposed themes to seminal bank
    for proposal in data.get("proposed_new_themes", []):
        if not proposal.get("theme_id"):
            continue
        db.insert_seminal_proposal({
            "bank_id":            generate_id("BANK"),
            "proposed_theme":     proposal.get("theme_id", ""),
            "problem_origin":     problem,
            "reason":             proposal.get("reason", ""),
            "suggested_keywords": proposal.get("suggested_keywords", []),
            "suggested_sources":  proposal.get("suggested_sources", []),
        })

    # -----------------------------------------------------------------------
    # Step 5 — Save decomposition + foundations document
    # -----------------------------------------------------------------------
    _save_doc(run_id, problem, data, sub_questions, decomp_data.get("decomposition_logic",""))

    # Print tree summary
    final_stats = tree.get_stats()
    gaps = tree.find_gaps()
    n_books  = sum(1 for s in data.get("seminal_works",[]) if s.get("material_type") == "book")
    n_papers = sum(1 for s in data.get("seminal_works",[]) if s.get("material_type") != "book")
    print(f"  [Grounder] {saved} seminal works saved ({n_papers} papers, {n_books} books) | "
          f"{len(data.get('themes_extracted',[]))} themes | "
          f"{len(data.get('proposed_new_themes',[]))} proposals to seminal bank")
    print(f"  [Grounder] Tree: {final_stats['total_nodes']} nodes | "
          f"{len(gaps)} gaps (unanswered questions / unsupported claims)")
    tree.close()
    logger.info("[Grounder] Complete")

    # F5: Flag sources that also appeared in a previous run (if set).
    try:
        prev_keys = db.get_previous_run_source_keys(run_id)
        if prev_keys:
            marked = db.mark_previously_seen(run_id, prev_keys)
            if marked:
                print(f"  [Grounder] {marked} sources flagged as previously seen")
                logger.info(f"[Grounder] {marked} sources marked as previously seen")
    except Exception as e:
        logger.debug(f"[Grounder] Could not check previous run: {e}")


# ---------------------------------------------------------------------------
# Document writer — fixed to actually populate all sections
# ---------------------------------------------------------------------------

def _save_doc(run_id: str, problem: str, data: dict,
              sub_questions: list = None, decomp_logic: str = ""):
    path = Path(__file__).parent.parent / "artifacts" / f"{run_id}_grounder_foundations.md"
    path.parent.mkdir(exist_ok=True)

    lines = [
        "# Foundations Document — Grounder",
        f"**Run:** {run_id}",
        f"**Problem:** {problem}",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "", "---", "",
    ]

    # Problem decomposition
    if sub_questions:
        lines += ["## Problem Decomposition", "", decomp_logic, ""]
        for sq in sub_questions:
            lines.append(f"- **{sq.get('id','?')}** [{sq.get('level','')}] {sq.get('question','')}")
            if sq.get("rationale"):
                lines.append(f"  *{sq['rationale']}*")
        lines.append("")

    # Themes
    themes = data.get("themes_extracted", [])
    lines += ["## Themes Extracted", ""]
    if themes:
        for t in themes:
            lines.append(f"- **{t.get('theme','')}**: {t.get('description','')}")
    else:
        lines.append("*(none extracted)*")
    lines.append("")

    # Fundamental Whys
    lines += ["## Fundamental Whys", ""]
    fw = data.get("fundamental_whys", "")
    lines.append(fw if fw else "*(not produced)*")
    lines.append("")

    # Intellectual Genealogy
    lines += ["## Intellectual Genealogy", ""]
    ig = data.get("intellectual_genealogy", "")
    lines.append(ig if ig else "*(not produced)*")
    lines.append("")

    # Original Definitions
    defs = data.get("original_definitions", [])
    lines += ["## Original Definitions", ""]
    if defs:
        for d in defs:
            lines.append(
                f"- **{d.get('concept','')}** "
                f"({d.get('defined_by','')}, {d.get('year','')}): "
                f"{d.get('definition','')}"
            )
    else:
        lines.append("*(none extracted)*")
    lines.append("")

    # Seminal Works — split books and papers
    works = data.get("seminal_works", [])
    lines += ["## Seminal Works", ""]
    books  = [w for w in works if w.get("material_type") == "book"]
    papers = [w for w in works if w.get("material_type") != "book"]

    if papers:
        lines += ["### Papers & Articles", ""]
        for w in sorted(papers, key=lambda x: x.get("year") or 9999):
            authors_str = ", ".join(w.get("authors", [])[:3])
            lines.append(
                f"- **[{w.get('year','n.d.')}] {w.get('title','')}**  "
                f"— {authors_str}"
            )
            lines.append(f"  *{w.get('seminal_reason','')}*")
            if w.get("active_link"):
                lines.append(f"  Link: {w['active_link']}")
            lines.append("")

    if books:
        lines += ["### Books", ""]
        for w in sorted(books, key=lambda x: x.get("year") or 9999):
            authors_str = ", ".join(w.get("authors", [])[:3])
            lines.append(
                f"- **[{w.get('year','n.d.')}] {w.get('title','')}**  "
                f"— {authors_str}"
            )
            lines.append(f"  *{w.get('seminal_reason','')}*")
            if w.get("active_link"):
                lines.append(f"  Link: {w['active_link']}")
            lines.append("")

    if not works:
        lines.append("*(none found)*")
        lines.append("")

    # Intersection Points
    intersections = data.get("intersection_points", [])
    lines += ["## Intersection Points", ""]
    if intersections:
        for i in intersections:
            themes_str = " × ".join(i.get("themes", []))
            lines.append(f"- **{themes_str}**: {i.get('description','')}")
    else:
        lines.append("*(none identified)*")
    lines.append("")

    # Assumptions Flagged
    assumptions = data.get("assumptions_flagged", [])
    if assumptions:
        lines += ["## Assumptions Flagged", ""]
        for a in assumptions:
            lines.append(f"- **{a.get('assumption','')}**: {a.get('note','')}")
        lines.append("")

    # Proposed New Themes
    proposals = data.get("proposed_new_themes", [])
    if proposals:
        lines += ["## Proposed New Themes (→ Seminal Bank)", ""]
        for p in proposals:
            lines.append(f"- **{p.get('theme_id','')}** ({p.get('label','')}): {p.get('reason','')}")
        lines.append("")

    path.write_text("\n".join(lines))
    logger.info(f"[Grounder] Foundations document saved: {path}")
