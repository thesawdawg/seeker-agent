# SEEKER Research Workflow — User Guide

This guide explains how to use SEEKER as a research instrument: what each agent does,
what it produces, and how to use the three **breaks** to steer a run toward the work
*you* want to do. SEEKER does not write your paper. It maps the intellectual territory
of a problem and hands you a traceable structure you can argue with.

---

## 1. The shape of a run

```
Concept Mapper  →  BREAK 0   (you confirm the conceptual territory)
Grounder  →  Social  →  Historian  →  Gaper
                →  BREAK 1   (you validate foundations, sources, and gaps)
Vision  →  Theorist  →  Rude  →  Synthesizer
                →  BREAK 2   (you set the trajectory and request outputs)
Thinker  →  Scribe (Understanding Map + your requested artifacts)
```

Start a run:

```bash
python3 main.py run --problem "Your research problem, stated as a problem — not a keyword"
```

Resume an interrupted or aborted run (nothing is recomputed — completed agents are skipped):

```bash
python3 main.py run --problem "..." --run-id RUN-YYYYMMDD-XXXX --resume
python3 main.py status --run-id RUN-YYYYMMDD-XXXX
```

Everything is persisted in `db/pipeline.db`; documents land in `artifacts/`, logs in
`logs/<run_id>.log`.

### How to state the problem

The Concept Mapper and Grounder both work from your problem statement, so its phrasing
determines the whole run. Good statements name a tension, not a topic:

- Weak: *"AI in education"*
- Strong: *"Why do adaptive learning systems improve measured outcomes while teachers
  report a loss of pedagogical judgement?"*

---

## 2. The argument tree (why outputs are traceable)

The backbone of a run is a persistent **argument tree** (`core/argument_tree.py`) that
grows as agents work. Nodes are the research question (`root`), sub-questions
(`question`), assertions (`claim`), verifiable sources (`evidence`), plus `bridge`,
`counter`, `historical`, `external`, and `audit_note` nodes.

Two consequences matter for you as a user:

1. **Every claim points at evidence.** Evidence is not limited to papers — books,
   reports, legal documents, court decisions, archival material, testimony, datasets,
   and news are all first-class evidence types. Claims are labelled `supported`,
   `contested`, `unsupported`, `weak`, `solid`, `contradicted`, or `bridged`.
2. **Gaps are proven, not guessed.** A question with no claims, or a claim with no
   evidence, *is* a gap by construction. The Gaper reads these off the tree before any
   model is asked for an opinion.

---

## 3. The agents

### Concept Mapper (pre-flight)
Translates your raw problem into its conceptual territory *before* any search happens,
in three layers: ConceptNet semantic expansion of your terms, a curated disciplinary
concept-cluster map (`concept_map.json`), and an LLM pass that catches what the static
map missed. Output: the set of **themes** (from `config.json`) activated for this run.
Themes decide which literatures get searched, so this is the highest-leverage decision
in a run — which is why Break 0 sits immediately after it. Expansions are cached, so
re-running a similar problem is cheap.

### Grounder — intellectual origins
Decomposes the problem into an exhaustive sub-question tree, turns each sub-question
into contextual search queries, searches OpenAlex, arXiv, Semantic Scholar, Google
Books, Open Library and web search, then synthesises the **intellectual genealogy**:
which works are seminal and *why*. Books are treated as first-class sources alongside
papers. This is the agent that creates the argument tree; sources are stored as
`seminal`.

### Social — contemporary state and bridges
Finds what is happening *now* and connects it back to the foundations. Sources include
OpenAlex, arXiv, PubMed, Semantic Scholar, CrossRef, JSTOR, PhilPapers, SSRN, CORE,
BASE, HAL, and Consensus (when configured). It also searches for **bridge papers** that
close temporal or disciplinary distances in the tree. Sources are stored as `current`,
and dead links are archived automatically. Social failing is non-fatal — the run
continues with fewer contemporary sources.

Social also runs standalone, independent of any run:

```bash
python3 main.py collect    # passive scan of all configured themes
python3 main.py recheck    # link-health check; flags dead seminal links for review
```

### Historian — chronology and audit
Starts from the Grounder's seminal works and extends forward in time: major phases and
what drove each transition, key actors and schools, methods evolution, turning points,
**dead ends**, and external factors (war, policy, funding shifts, institutional change).
It also **audits** the tree, marking branches as solid or shaky so downstream agents
know what they can lean on.

### Gaper — mapping absence
Produces the gap map in three steps:
1. **Structural gaps** read deterministically off the tree (unanswered questions,
   unsupported claims, low-confidence claims, temporal gaps needing bridges).
2. **Analytical gaps** from a two-pass LLM analysis: disciplinary silences,
   methodological blind spots, unquestioned assumptions, dead ends worth revisiting.
3. **Merge**, with structural gaps taking priority because they are proven. The LLM is
   not allowed to dismiss a structural gap.

Each gap carries a `gap_id`, a type, and a significance rating — the identifiers you use
when correcting things at Break 1.

### Vision — logical consequences
The first *inferential* agent, and the first to run after Break 1. Extracts what
necessarily follows from the established picture: direct implications, logical chains,
second-order consequences, hidden assumptions that would collapse current understanding
if false, what each gap logically demands, and outright logical contradictions. Each
implication is traceable to its source finding and rated **Strong / Moderate /
Speculative**. Vision proposes nothing — it only draws consequences.

### Theorist — concrete proposals
The first constructive agent. Turns gaps and implications into concrete approaches,
frameworks, and solutions, each anchored in pipeline outputs and typed as `novel`,
`extension`, `revival`, or `hybrid`, with a promise rating. It works in two passes (an
index first, then full detail per proposal) so a long output can never silently discard
every proposal.

### Rude — adversarial feasibility
The counterweight to the Theorist. Rude evaluates each proposal *strictly on empirical
evidence*: has the core mechanism actually been demonstrated, not merely theorised? It
cross-references the Historian's dead ends (if this was tried and failed, what exactly
happened, and is the revival justified?) and the most recent results from Social. It
does not care about elegance or novelty. Each evaluation yields a **verdict**, a reason,
and the **weakest empirical link** — often the single most useful line in a run.

### Synthesizer — the research narrative
Integrates everything into one coherent argument rather than a summary: a sharpened
problem statement, genealogy, historical trajectory, what is known / contested /
unknown, the gap landscape, the logical demands, the proposals that survived Rude,
explicit tensions where agents disagreed, every Break 1 override you made, and a closing
trajectory statement. This narrative is what you read at Break 2.

### Thinker — new directions
Runs after Break 2 and is the only agent allowed to look beyond the current frame — from
a position of deep knowledge, not brainstorming. Produces new directions, alternative
framings, adjacent fields and methods to bring into contact, second-generation
questions, underexplored combinations of findings, and challenges to assumptions that
survived the whole pipeline. Each direction is labelled by distance: **Near / Mid /
Far**.

### Scribe — artifacts
Formats outputs for a stated audience and writes real files into `artifacts/`.

- **Understanding Map** (`.md`) — generated on *every* run, whether or not you asked for
  it. It is not a summary of findings; it is a guided tour of the territory designed to
  get you to genuine comprehension, with every substantive assertion carrying a
  `[CiteKey]` drawn from a manifest of sources the pipeline actually retrieved. A
  companion references file is written alongside it.
- **On request** (via Break 2): `research_brief`, `blog_post`, `internal_memo` (Markdown)
  and `literature_review`, `paper_section`, `grant_background` (LaTeX body).

---

## 4. The breaks — where you steer

A break is a hard stop. SEEKER writes a review document to
`artifacts/<run_id>_break<N>_review.md`, pauses, and waits. You fill in the
`**Your instructions:**` section at the bottom of that document and press ENTER (or give
the path to a separate instruction file). Your instructions are injected verbatim into
the next agent's context.

If an instruction contradicts what an agent concluded, SEEKER does **not** overrule you —
it records a `CONTRADICTION NOTICE` in the instruction stream so downstream agents (and
the final synthesis) know that human judgement diverged from pipeline logic, and why.
Breaks are recorded in the database, so a resumed run does not re-ask you.

Writing `CONFIRMED` is always a valid answer. Free-form prose is always allowed
alongside the structured directives below.

### Break 0 — Theme confirmation (before any search)
**You see:** the themes the Concept Mapper activated, and the themes it excluded with
reasons.

**Use it to:** aim the search. This is the cheapest possible place to fix a run — every
subsequent agent searches inside the territory you approve here. Add the literature the
mapper did not know was relevant; drop themes that will flood the run with noise.

```
CONFIRMED
ADD THEME: <theme_id>
REMOVE THEME: <theme_id>
```

### Break 1 — Ground truth validation (after Grounder/Social/Historian/Gaper)
**You see:** the seminal works with the reason each was judged seminal, the historical
map with phase tags, and the full gap map with IDs, types, and significance.

**Use it to:** correct the factual and interpretive base before anything is inferred
from it. This is where your domain expertise pays for itself: a wrongly-seminal work or
a mischaracterised gap otherwise propagates through Vision, Theorist, Rude, and the
synthesis. Removing a well-supported gap is allowed and logged as a contradiction
notice.

```
CONFIRMED
CORRECT GAP <gap_id>: <your correction>
REMOVE GAP <gap_id>
ADD GAP: <description>
OVERRIDE SEMINAL <source_id>: <your note>
```

Also a good place for free-form scoping: *"treat the post-2015 policy literature as out
of scope"*, *"prioritise the methodological blind spot over the temporal gaps"*.

### Break 2 — Trajectory evaluation (after the Synthesizer)
**You see:** the sharpened problem statement, the research narrative, the trajectory
statement, key tensions, and Rude's verdict on every proposal with its weakest empirical
link.

**Use it to:** decide what this run is *for*. You are choosing the direction Thinker
expands on, overriding feasibility verdicts you disagree with, and naming the artifacts
you actually need.

```
CONFIRMED
OVERRIDE VERDICT <evaluation_id>: <your reasoning>
SCRIBE OUTPUT: research_brief   | audience: collaborators
SCRIBE OUTPUT: paper_section    | audience: specialists
SCRIBE OUTPUT: literature_review | audience: specialists
SCRIBE OUTPUT: grant_background | audience: funders
SCRIBE OUTPUT: blog_post        | audience: general public
SCRIBE OUTPUT: internal_memo    | audience: lab
```

Request as many outputs as you like — one line each, one artifact per line. With no
`SCRIBE OUTPUT` line you get a `research_brief` for a researcher audience. The
Understanding Map is produced either way.

---

## 5. Using breaks well

- **Read the review document before answering.** The breaks are the point of the system:
  they are where you learn the field, not merely approve it.
- **Spend your effort early.** A theme fixed at Break 0 costs one line; the same problem
  discovered at Break 2 costs a whole run.
- **Override freely, and say why.** The contradiction log turns your disagreement into
  part of the record rather than a silent edit — the final synthesis reports where your
  judgement diverged from the pipeline's.
- **Use the breaks as an audit trail.** Review documents are kept per run in
  `artifacts/`, so a finished run shows not just what SEEKER concluded but every point at
  which a human intervened.
- **Aborted runs are cheap to restart.** If a step fails, the run is marked
  `failed:<Step>`; re-run with `--run-id ... --resume` and only the missing steps
  execute.

---

## 6. Where things end up

| Location | Contents |
|---|---|
| `artifacts/<run_id>_break{0,1,2}_review.md` | Break review documents plus your instructions |
| `artifacts/<run_id>_understanding_map.md` | Understanding Map (+ `_refs.tex` references) |
| `artifacts/<run_id>_<output_type>.{md,tex}` | Artifacts requested at Break 2 |
| `db/pipeline.db` | Sources, tree, gaps, implications, proposals, evaluations, syntheses, directions, artifacts |
| `logs/<run_id>.log` | Full run log |

Use `python3 main.py status --run-id <run_id>` for a per-run count of each of those
tables, and `python3 main.py bank` to review accumulated seminal-bank proposals.
