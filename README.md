
<div align="center">

# 📜 [(BETA) - Submit a Research Question →](https://anvix9.github.io/request)

### **Free public testing - get an Understanding Map for your research question, by email, within 1-2 days.**

> Open to social sciences · humanities · philosophy · law · computing · interdisciplinary studies.
> *Medical & clinical questions coming soon.*

[**→ Open the request form**](https://anvix9.github.io/request) &nbsp; · &nbsp; [**📝 Already received your document? Share feedback →**](https://anvix9.github.io/feedback)

---

</div>

# SEEKER — Multi-Agent Deep Research Intelligence Pipeline

**SEEKER** is a 10-agent research pipeline that conducts deep, traceable academic research by orchestrating specialized agents across live data sources. It builds a persistent **argument tree** where every claim traces to verifiable evidence — papers, books, legal documents, news articles, court decisions, testimony, or archival records.

The system is designed to preserve the researcher's intellectual contribution. It does not write your paper. It maps the intellectual territory so you can navigate it yourself.

## How It Works

```
Concept Mapper → Break 0 (you confirm themes)
  → Grounder (decomposes problem → searches → builds argument tree)
  → Social (contemporary + bridge papers → extends tree)
  → Historian (audits tree → synthesizes historical context → external factors)
  → Gaper (structural + analytical gap mapping from tree)
  → Break 1 (you review foundations and gaps)
  → Vision (logical implications)
  → Theorist (research proposals)
  → Rude (feasibility evaluation — adversarial)
  → Synthesizer (research narrative)
  → Break 2 (you set trajectory)
  → Thinker (new directions)
  → Scribe (understanding map + outputs)
```

**Three human-in-the-loop breaks** where you read, learn, and decide the trajectory. The pipeline stops and waits for your input.

**The Argument Tree** is the backbone. Every agent reads and extends it. Claims without evidence are flagged. Temporal gaps are detected and bridge papers are searched automatically. The Historian audits the tree for solidity before downstream agents use it.

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/anvix9/basis_research_agents.git
cd basis_research_agents
uv sync --locked
```

### 2. Set up the ConceptNet database

The ConceptNet database provides semantic concept expansion. It ships compressed:

```bash
cd db/
gunzip conceptnet.db.gz
cd ..
```

This produces `db/conceptnet.db` (~184 MB). If you skip this step, the concept mapper will use a simpler keyword-only mode.

### 3. Configure a model provider

```bash
cp .env.example .env
```

SEEKER talks to **any OpenAI-compatible endpoint**. The default is
[Open-WebUI](https://docs.openwebui.com/), using your personal API key from
**Settings → Account**:

```
OPENWEBUI_BASE_URL=http://localhost:3000/api
OPENWEBUI_API_KEY=sk-...
```

Then set which models to use in `config.json` under
`llm.providers.open-webui.models`:

```json
"models": { "primary": "qwen3:32b", "light": "llama3.2:3b" }
```

**One working provider is all you need.** All academic sources (OpenAlex,
Semantic Scholar, arXiv, CORE) are free and keyless.

Check what is reachable and how each agent will route:

```bash
python3 main.py keys
```

### 4. Run your first pipeline

```bash
python3 main.py run --problem "How does identity influence intelligence through the lens of identity theory?"
```

The pipeline will:
1. Expand your problem into conceptual territory
2. Stop at **Break 0** — you confirm which academic themes to search
3. Search 10+ academic sources, build the argument tree
4. Stop at **Break 1** — you review the foundations and gaps
5. Generate implications, proposals, evaluations, synthesis
6. Stop at **Break 2** — you set the trajectory and choose outputs
7. Produce an Understanding Map, research brief, and whatever else you requested

### 5. Resume a run

The pipeline is a **resumable state machine**. Every step is a record with an
explicit status, so a run can be interrupted, inspected and continued.

```bash
# See exactly where a run stands
python3 main.py steps --run-id RUN-20260407-022355-242D

# Continue — retries a failed step, or re-asks an unanswered break
python3 main.py run --run-id RUN-20260407-022355-242D --resume
```

A resume never steps over a failed step, and never re-asks a break you have
already answered — your instructions are stored and replayed.

### 6. Re-run a step

To redo a step and everything that depended on it:

```bash
python3 main.py rerun --run-id RUN-... --step gaper
```

This **discards** that step's output and all downstream output, then leaves
the run ready to resume. It lists what will be lost and asks first. Use
`--only` to reset a single step without touching later ones.

Steps, in order:

```
concept_mapper  break0  grounder  social  historian  gaper  break1
vision  theorist  rude  synthesizer  break2  thinker  scribe
```

## Web Interface

The CLI drives the pipeline for one person on one machine. The web interface
serves multiple researchers, each using their own model provider and API key.

### With Docker (recommended)

```bash
cp .env.example .env
echo "SEEKER_SECRET_KEY=$(python3 -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env

docker compose up -d
```

Then open **http://localhost:8000**.

Five services: `mysql`, `redis`, a one-shot `conceptnet` unpacker, `web`, and
`worker`. Compose **refuses to start without `SEEKER_SECRET_KEY`** rather than
leaving users' provider keys unprotected.

```bash
docker compose logs -f worker      # watch the pipeline
docker compose up -d --scale worker=3   # more concurrent runs
docker compose down                # stop; volumes and data persist
```

> **Reaching a provider on your host.** Containers cannot see your host's
> `localhost`. If Open-WebUI runs on the host machine, sign in with the Docker
> gateway address rather than `localhost` — e.g.
> `http://172.17.0.1:3000/api`, or `http://host.docker.internal:3000/api` on
> Docker Desktop. A provider running in another container is reachable by its
> service name.

### Without Docker

```bash
uv sync --locked --extra web
export SEEKER_SECRET_KEY=$(uv run python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Terminal 1 — HTTP API (never runs pipeline work itself)
uv run uvicorn web.app:app --host 0.0.0.0 --port 8000

# Terminal 2 — worker (claims jobs and advances runs)
uv run python worker.py
```

Defaults to SQLite and in-process sessions, which is fine for one web process.
Set `MYSQL_URL` and `REDIS_URL` for anything larger.

### How it fits together

```
browser ──poll──> web app ──enqueue──> jobs table <──claim── worker
                     │                                         │
                     └──────────── MySQL / SQLite ─────────────┘
```

The web app writes to the database and queues a job; a **worker** process
advances the run. Reaching a break parks the run and releases the worker, so a
run waiting a day for a human occupies nothing. Restarting the web app never
interrupts a pipeline, and a crashed worker's jobs are reaped and retried.

### Sign-in

There are no passwords. A user signs in with the API key for their own model
provider — Open-WebUI by default. The key is validated against the provider,
then identifies the account by fingerprint.

On the first run you also choose which model fills each **role**: `primary`
for the heavy reasoning agents, `light` for Social and Scribe. Agents pick a
role rather than a model name, so routing survives changing provider. These are
saved against your provider and reused.

Provider keys are **encrypted at rest** with `SEEKER_SECRET_KEY` and never
returned by the API; endpoints expose only a masked hint. Without that
variable the app refuses to store a key rather than keeping it in plaintext.

Authentication is one function — `web/auth.py:resolve_user`. A SAML
integration replaces it and leaves every route, model and worker untouched.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/login` | Validate a provider key, start a session |
| `GET` | `/api/auth/me` | Current user and configured providers |
| `PUT` | `/api/credentials` | Add/replace a provider's endpoint + key |
| `GET` | `/api/models` | Models the user's provider serves |
| `PATCH` | `/api/credentials/{provider}/models` | Set the primary/light roles |
| `GET` | `/api/runs` | The caller's runs |
| `POST` | `/api/runs` | Start a run |
| `GET` | `/api/runs/{id}/status` | **Polled** — step-level progress |
| `GET` | `/api/runs/{id}/break/{n}` | Structured break content |
| `POST` | `/api/runs/{id}/break/{n}` | Answer a break, release the run |
| `POST` | `/api/runs/{id}/stop` | Stop a run mid-step |
| `POST` | `/api/runs/{id}/resume` | Resume, optionally with new models |
| `GET` | `/api/runs/{id}/steps/{s}/impact` | What a re-run would discard |
| `POST` | `/api/runs/{id}/steps/{s}/rerun` | Re-run a step and its dependents |
| `GET` | `/api/runs/{id}/artifacts` | Scribe outputs |

Interactive docs at `/docs` once the server is running.

### The interface

Open `http://localhost:8000`. The UI is plain HTML, CSS and JavaScript served
by the same FastAPI app — **no build step, no npm, no external assets**, and it
follows your system light/dark preference.

| Screen | What it does |
|---|---|
| Sign in | Provider, base URL, API key. Presets fill the URL per provider. |
| Runs | Your runs with live status pills and step counts |
| New run | Problem statement, provider, optional per-agent model routing |
| Run | 14-step progress rail, break screens, artifacts |

The **progress rail** shows every step with its state — a spinner while
running, ✓ done, ⏸ waiting on you, ✗ failed, ⊘ skipped. It polls
`/api/runs/{id}/status` every 2s while work is moving and backs off to 15s once
a run parks at a break, because nothing changes there until you act. Polling
stops entirely when the tab is hidden.

Hovering a completed step reveals a **↻ re-run** button. It asks
`/api/runs/{id}/steps/{step}/impact` first and shows exactly which completed
steps will be discarded before you confirm — re-running Grounder throws away
the whole run below it.

### Stopping a run

A run using the wrong model does not have to be waited out. **Stop and change
model** on the live card halts it, and a resume panel offers the model pickers.

Stopping is cooperative: the request is recorded, and the worker unwinds at
its next checkpoint — normally the next model call, so it takes effect within
one call rather than at the end of the step. The interrupted step is
discarded so it restarts cleanly; every step completed before it is kept.

```bash
curl -X POST .../api/runs/{id}/stop
curl -X POST .../api/runs/{id}/resume \
  -d '{"models": {"primary": "qwen3:32b", "light": "llama3.2:3b"}}'
```

### Breaks in the browser

Each break renders as structured widgets over the same data the CLI writes to
markdown:

| Break | Widgets |
|---|---|
| **0** Themes | Checkbox per theme with its keyword seeds |
| **1** Foundations | Per-gap remove/restore and correct; per-source override notes; the historical map |
| **2** Trajectory | Narrative and trajectory; per-verdict override boxes; an output builder (type + audience) |

Every widget generates a line of the **same directive language the CLI
accepts** — `REMOVE GAP GAP-3`, `SCRIBE OUTPUT: blog_post | audience: general
public` — and each screen shows a live preview of the exact text being
submitted, so the interface stays honest about what it does on your behalf.
A run answered in a browser is indistinguishable downstream from one answered
at a terminal.

Every break also carries a **model panel** for the agents that have not run
yet. A break is the safe point to change routing mid-pipeline; agents that have
already run are shown disabled.

Free-form guidance can accompany any break, and is appended after the
generated directives.

## Database Backends

SEEKER runs on either backend. All pipeline state goes through
`core/database.py`, and the SQL dialect lives in `core/db_backend.py`.

| Backend | When | Setup |
|---|---|---|
| **SQLite** (default) | local CLI use, tests | none — `db/pipeline.db` |
| **MySQL** | multi-user / Docker deployment | set `MYSQL_URL` or `MYSQL_*` |

Selection order: `SEEKER_DB_BACKEND=sqlite|mysql`, else MySQL if `MYSQL_HOST`
or `MYSQL_URL` is set, else SQLite.

```bash
# MySQL, via a single URL
export MYSQL_URL=mysql://seeker:secret@localhost:3306/seeker

# or discrete variables
export MYSQL_HOST=localhost MYSQL_USER=seeker \
       MYSQL_PASSWORD=secret MYSQL_DATABASE=seeker
```

MySQL needs the optional driver set: `uv sync --locked --extra mysql`.

`db/conceptnet.db` stays SQLite on both — it is a read-only reference corpus,
not pipeline state.

### Running the MySQL tests

They skip unless pointed at a scratch database. **Never aim them at a database
holding real runs** — they clear every table.

```bash
export SEEKER_TEST_MYSQL_URL=mysql://user:pass@127.0.0.1:3306/seeker_test
pytest tests/test_mysql_backend.py
```

## Model Providers

Providers are declared in `config.json` under `llm.providers`. Each one is just
a `base_url`, an API key, and the models it serves — so any OpenAI-compatible
endpoint works without code changes.

| Provider | `base_url` | Notes |
|---|---|---|
| `open-webui` | `http://localhost:3000/api` | **Default.** Key from Settings → Account |
| `ollama` | `http://localhost:11434/v1` | Ollama's OpenAI-compatible route |
| `lm-studio` | `http://localhost:1234/v1` | |
| `vllm` | `http://localhost:8000/v1` | |
| `openai` | `https://api.openai.com/v1` | |
| `openrouter` | `https://openrouter.ai/api/v1` | |
| `anthropic` | `https://api.anthropic.com` | One provider among many |

Every `base_url` and key can be overridden by environment variable
(`OPENWEBUI_BASE_URL`, `OLLAMA_BASE_URL`, …), so deployments never edit
`config.json`.

### Routing

`llm.fallback_chain` is tried in order until a provider answers:

```json
"fallback_chain": [ { "provider": "open-webui" }, { "provider": "ollama" } ]
```

Each agent picks a **model role** rather than a model name, so routing survives
a provider swap. Heavy reasoning agents use `primary`; Social and Scribe use
`light`. Override per agent in `llm.agents` with an explicit `model`,
`provider`, `max_tokens`, or `temperature`.

### Using Ollama directly

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:14b
```

Set `OLLAMA_BASE_URL=http://localhost:11434/v1` in `.env` and put `ollama`
first in `fallback_chain`. No API key is needed.

### What to expect from local models

Local models (7B–14B) produce usable results but with lower quality than large
frontier models:
- Sub-question decomposition may be shallower
- JSON parsing failures are more frequent (the pipeline has fallback parsers)
- Synthesis quality depends heavily on model size
- 14B+ is recommended; 7B works but produces thin outputs

The pipeline is designed to degrade gracefully — every JSON parser has a fallback, every agent has error handling, and the argument tree preserves whatever was successfully parsed.

## Configuration

### `config.json`

Controls which academic themes are available, their search keywords, and which sources each agent uses:

```json
{
  "agent_sources": {
    "social":   ["openalex", "arxiv", "pubmed", "semantic_scholar", "core"],
    "grounder": ["openalex", "semantic_scholar"]
  }
}
```

Add or remove sources per agent without touching code. Per-run source choices are
resolved into this routing before a worker advances the run. Grounder and Social
send every academic-provider query through the shared dispatch guard, which checks
both `sources.<id>.enabled` and the agent's resolved allowlist. A deselected
provider therefore receives no query from that run, including Break 0 coverage
previews and Social bridge searches.

Historian is currently synthesis-only: it reads Grounder and Social evidence,
calls the configured model, and validates citation links. It is deliberately
absent from `agent_sources` and must not be presented as querying OpenAlex,
Semantic Scholar, or Consensus. Link validation is separate from provider
retrieval.

### `concept_map.json`

37 semantic concept clusters that translate natural language research problems into academic themes. If SEEKER doesn't recognize your field, add a cluster here (see CONTRIBUTING.md).

## Evaluation Tools

```bash
# Check if references are real (Semantic Scholar + OpenAlex + Claude web search)
python3 tools/eval_references.py --run-id RUN-... --types seminal --limit 10

# Check if claims about references are accurate
python3 tools/eval_claims.py --run-id RUN-... --types seminal --limit 10

# Generate a formatted reference section (APA/Chicago)
python3 tools/generate_references.py --run-id RUN-... --format apa
```

## Academic Sources

SEEKER searches 10+ live academic sources. No source uses model training data.

| Source | Key Required | Coverage |
|--------|-------------|----------|
| OpenAlex | No (email for faster access) | 250M+ works, full metadata |
| Semantic Scholar | No (key for higher limits) | 200M+ papers, citation graphs |
| arXiv | No | Preprints (CS, physics, math, etc.) |
| PubMed/NCBI | No | Biomedical literature |
| CORE | No | 200M+ open access papers |
| PhilPapers | No | Philosophy-specific |
| PhilArchive | No | Open philosophy preprints |
| PhilSci Archive | No | Philosophy of science |
| Scopus | API key (institutional) | Elsevier's comprehensive index |
| Consensus | MCP OAuth (Pro plan) | Semantic search, 200M+ papers |

## Project Structure

```
basis_research_agents/
├── main.py              # Pipeline runner and CLI
├── config.json          # Themes, sources, agent configuration
├── concept_map.json     # 37 semantic concept clusters
│
├── agents/              # The 10 pipeline agents
│   ├── grounder.py      # Problem decomposition + argument tree building
│   ├── social.py        # Contemporary + bridge paper discovery
│   ├── historian.py     # Tree audit + historical context + external factors
│   ├── gaper.py         # Tree-native structural + analytical gap mapping
│   ├── vision.py        # Logical implications extraction
│   ├── theorist.py      # Research proposal generation (two-pass)
│   ├── rude.py          # Adversarial feasibility evaluation
│   ├── synthesizer.py   # Research narrative synthesis
│   ├── thinker.py       # New direction exploration
│   └── scribe.py        # Understanding Map + output generation
│
├── core/                # Infrastructure
│   ├── argument_tree.py # Persistent argument tree (TreeBuilder class)
│   ├── llm.py           # LLM router (any OpenAI-compatible provider + fallback chain)
│   ├── db_backend.py    # SQL dialect + connections (sqlite | mysql)
│   ├── pipeline.py      # resumable state machine (steps, breaks, re-runs)
│   ├── jobs.py          # DB-backed job queue claimed by workers
│   ├── users.py         # users, provider credentials, run ownership
│   └── crypto.py        # encrypts stored provider API keys
│   ├── database.py      # SQLite schema (12 tables)
│   ├── context.py       # Context assembly with tree injection
│   ├── concept_mapper.py# Problem → theme activation (130 disciplines)
│   ├── breaks.py        # Human-in-the-loop break points
│   ├── references.py    # Reference formatting for Scribe
│   ├── consensus_mcp.py # Consensus OAuth 2.1 MCP client
│   ├── rate_limiter.py  # Per-source API rate limiting
│   ├── keys.py          # Environment variable management
│   └── utils.py         # Shared utilities
│
├── tools/               # Evaluation and export tools
│   ├── eval_references.py    # Reference existence verification
│   ├── eval_claims.py        # Claim accuracy verification
│   ├── generate_references.py# APA/Chicago reference section generator
│   ├── export_seminal.py     # Seminal works → Jekyll blog export
│   └── import_conceptnet.py  # Build ConceptNet DB from CSV dump
│
├── tests/               # Unit tests
│   ├── test_grounder_tree.py # Argument tree building
│   ├── test_social_bridge.py # Bridge gap detection
│   ├── test_historian_tree.py# Tree audit + extension
│   ├── test_context_tree.py  # Tree context injection
│   ├── test_gaper_tree.py    # Tree-native gap mapping
│   └── test_references.py    # Reference section generation
│
├── db/                  # Databases
│   ├── conceptnet.db.gz # ConceptNet (compressed — gunzip before first run)
│   ├── pipeline.db      # Created automatically on first run
│   └── consensus_tokens.json  # OAuth tokens (auto-managed)
│
├── artifacts/           # Pipeline outputs (per run)
├── logs/                # Run logs
├── .env.example         # API key template
├── CONTRIBUTING.md      # Contribution guide
├── LICENSE              # MIT
├── pyproject.toml        # Project metadata and dependency declarations
└── uv.lock               # Reproducible Python dependency lock
```

## The Argument Tree

The argument tree is the single source of truth for the entire pipeline. Every agent reads and extends it.

```
ROOT: "How does identity influence intelligence?"
├── Q1: What is identity?
│   ├── CLAIM [solid] (90%): Identity is socially constructed
│   │   ├── EVIDENCE [book]: Mead 1934 — Mind, Self, and Society
│   │   ├── EVIDENCE [paper]: Tajfel 1979 — Social identity
│   │   └── COUNTER: Essentialists argue biological basis
│   └── BRIDGE [temporal]: Stryker 1980 connects 1934→1995
├── Q2: What is intelligence?
│   └── CLAIM [weak] (40%): Intelligence is g-factor — single evidence
├── Q3: How are they related? [UNANSWERED — structural gap]
├── HISTORICAL: Locke 1690 — personal identity tied to memory
├── EXTERNAL [war]: WWII disrupted European psychology departments
└── AUDIT: Tree health — 2 solid, 1 weak, 1 unanswered
```

Node types: `root`, `question`, `claim`, `evidence`, `bridge`, `counter`, `historical`, `external`, `audit_note`

Evidence types: `paper`, `book`, `report`, `legal_document`, `court_decision`, `news_article`, `archival`, `testimony`, `resolution`, `dataset`

## Tests

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run individually
python3 tests/test_grounder_tree.py
python3 tests/test_social_bridge.py
python3 tests/test_historian_tree.py
python3 tests/test_context_tree.py
python3 tests/test_gaper_tree.py
python3 tests/test_references.py
```

## CLI Reference

```bash
# Full pipeline run
python3 main.py run --problem "Your research question"

# Resume from where you left off
python3 main.py run --problem "..." --run-id RUN-XXXXXXXX --resume

# Test a single source handler
python3 main.py test --source openalex --query "neural networks"

# Passive collection (Social only, no pipeline)
python3 main.py collect

# Check run status
python3 main.py status --run-id RUN-XXXXXXXX

# View seminal bank proposals
python3 main.py bank
```

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

Thanks to Academic data from OpenAlex, Semantic Scholar, arXiv, PubMed, CORE, PhilPapers, and Consensus, and Claude (Anthropic), Ollama local model fallback. 
