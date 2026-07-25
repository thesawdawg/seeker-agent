"""
Pipeline Runner
---------------
CLI entry point. The pipeline itself lives in core/pipeline.py as a resumable
state machine; this module drives it, answering breaks at the terminal.

  Concept Mapper → Break 0 (theme confirmation)
  → Grounder (builds argument tree) → Social (contemporary + bridges)
  → Historian (audit + external factors) → Gaper (tree-native gap mapping)
  → Break 1 → Vision → Theorist → Rude → Synthesizer
  → Break 2 → Thinker → Scribe

Usage:
  python3 main.py run  --problem "Your research problem here"
  python3 main.py run  --run-id RUN-20260330-XXXX --resume            (resume)
  python3 main.py steps   --run-id RUN-...                     (per-step status)
  python3 main.py rerun   --run-id RUN-... --step grounder     (re-run a step)
  python3 main.py collect                                    (Social passive scan)
  python3 main.py recheck                                     (link health check)
  python3 main.py status  --run-id RUN-20260330-XXXX            (check run status)
  python3 main.py bank                                (seminal bank proposals)
"""

import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime

# Ensure pipeline root is in path
sys.path.insert(0, str(Path(__file__).parent))

# Load .env FIRST — before any module that reads os.environ at import time
from core.keys import _load_env
_load_env()

from core.utils    import setup_logging, generate_run_id, load_config
from core          import database as db
from core          import breaks
from core          import pipeline
from agents.social import collect as social_collect
from agents.social import recheck_links


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "pending":        "·",
    "running":        "▶",
    "awaiting_input": "⏸",
    "done":           "✓",
    "failed":         "✗",
    "skipped":        "⊘",
}


def _print_steps(state: dict):
    print(f"\n  {'─'*60}")
    print(f"  Steps — {state['progress']['done']}/{state['progress']['total']} complete")
    print(f"  {'─'*60}")
    for step in state["steps"]:
        icon = _STATUS_ICON.get(step["status"], "?")
        line = f"  {icon}  {step['label']:<34} {step['status']}"
        if step.get("error"):
            line += f"\n       └─ {step['error'][:100]}"
        print(line)
    print(f"  {'─'*60}\n")


# ---------------------------------------------------------------------------
# Main pipeline — drive the state machine, answering breaks at the terminal
# ---------------------------------------------------------------------------

def run_pipeline(problem: str = None, run_id: str = None, resume: bool = False):
    logger = logging.getLogger("pipeline")

    db.init_db()
    config = load_config()

    existing = db.get_run(run_id) if run_id else None
    if existing and not resume:
        print(f"Run {run_id} already exists. Use --resume to continue.")
        return
    if existing:
        problem = problem or existing.get("problem", "")
    else:
        if not problem:
            print("A new run needs --problem.")
            return
        run_id = pipeline.create_run(problem, run_id)

    log = setup_logging(run_id)

    print(f"\n{'='*60}")
    print(f"  MULTI-AGENT RESEARCH PIPELINE")
    print(f"{'='*60}")
    print(f"  Run ID:  {run_id}")
    print(f"  Problem: {problem[:70]}{'...' if len(problem) > 70 else ''}")
    print(f"  Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Storage: {db.backend_name()}")
    print(f"{'='*60}\n")

    # Drive: advance until a break, then answer it here and continue.
    while True:
        state = pipeline.advance(run_id, problem, config)

        if state["failed_steps"]:
            _print_steps(state)
            _abort(run_id, state["failed_steps"][0])
            return

        if state["awaiting_break"] is not None:
            _print_steps(state)
            breaks.answer_break_interactively(run_id, state["awaiting_break"], config)
            continue

        if state["complete"]:
            break

    _print_steps(pipeline.get_state(run_id))

    artifacts = db.get_artifacts(run_id)
    print(f"{'='*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"{'='*60}")
    print(f"  Run ID:    {run_id}")
    print(f"  Artifacts: {len(artifacts)} produced")
    for art in artifacts:
        print(f"    → [{art.get('output_type')}] {art.get('file_path','')}")
    print(f"  Logs:      logs/{run_id}.log")
    print(f"{'='*60}\n")
    logger.info(f"Pipeline complete: {run_id}")


def _abort(run_id: str, step: str):
    logging.getLogger("pipeline").error(f"Pipeline aborted at: {step}")
    print(f"\n  Pipeline aborted at step: {step}")
    print(f"  Run ID saved: {run_id}")
    print(f"  Resume with:  python3 main.py run --run-id {run_id} --resume")
    print(f"  Or re-run just that step:")
    print(f"                python3 main.py rerun --run-id {run_id} --step {step}")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_run(args):
    # Confirm at least one LLM provider can serve the pipeline before starting
    from core import llm as llm_module
    plan = llm_module.get_client().describe_plan("grounder")
    if not plan:
        print("\n  ⚠️  WARNING: no LLM provider is configured.")
        print("     Set a provider in config.json under llm.providers and give it")
        print("     a base_url + api key in .env — by default OPENWEBUI_BASE_URL")
        print("     and OPENWEBUI_API_KEY. Run 'python3 main.py keys' for details.")
        print("     Continuing in 5 seconds...\n")
        import time; time.sleep(5)
    else:
        chain = " → ".join(f"{s['provider']}:{s['model']}" for s in plan)
        print(f"\n  ✅  LLM chain: {chain}")
    if not args.problem and not args.run_id:
        print("\n  A new run needs --problem, or pass --run-id to resume one.\n")
        return
    run_pipeline(
        problem=args.problem,
        run_id=args.run_id,
        resume=args.resume
    )


def cmd_steps(args):
    """Show per-step progress for a run."""
    db.init_db()
    state = pipeline.get_state(args.run_id)
    if not state.get("exists"):
        print(f"Run not found: {args.run_id}")
        return
    print(f"\n  Run:     {args.run_id}")
    print(f"  Problem: {state['problem'][:70]}")
    print(f"  Status:  {state['status']}")
    _print_steps(state)
    if state["awaiting_break"] is not None:
        print(f"  ⏸  Waiting on Break {state['awaiting_break']}.")
        print(f"     Answer it with: python3 main.py run --run-id {args.run_id} --resume\n")


def cmd_rerun(args):
    """Re-run a completed step, discarding it and everything downstream."""
    db.init_db()
    if args.step not in pipeline.STEP_BY_NAME:
        print(f"Unknown step: {args.step}")
        print(f"Valid steps: {', '.join(s.name for s in pipeline.STEP_DEFS)}")
        return

    state = pipeline.get_state(args.run_id)
    if not state.get("exists"):
        print(f"Run not found: {args.run_id}")
        return

    affected = [args.step] + ([] if args.only else pipeline.downstream_steps(args.step))
    done = {s["step_name"] for s in state["steps"]
            if s["status"] in ("done", "skipped")}
    discarding = [n for n in affected if n in done]

    print(f"\n  Re-running '{args.step}' for {args.run_id}")
    if discarding:
        print(f"  This DISCARDS the output of {len(discarding)} completed step(s):")
        for name in discarding:
            print(f"    - {pipeline.STEP_BY_NAME[name].label}")
    else:
        print("  No completed steps will be discarded.")

    if not args.yes:
        answer = input("\n  Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Cancelled.\n")
            return

    reset = pipeline.reset_step(args.run_id, args.step, cascade=not args.only)
    print(f"\n  Reset {len(reset)} step(s). Continue with:")
    print(f"    python3 main.py run --run-id {args.run_id} --resume\n")


def cmd_collect(args):
    """Run Social passive collection."""
    setup_logging("social-collect")
    db.init_db()
    config = load_config()
    print("\n▶  Social passive collection starting...")
    summary = social_collect(config)
    print(f"\n  ✓  Collection complete:")
    print(f"     Themes scanned:    {summary['themes_scanned']}")
    print(f"     Sources collected: {summary['sources_collected']}")
    print(f"     Dead links:        {summary['dead_links']}")


def cmd_recheck(args):
    """Run link health check."""
    setup_logging("link-recheck")
    db.init_db()
    print("\n▶  Link health recheck starting...")
    summary = recheck_links()
    print(f"\n  ✓  Recheck complete:")
    print(f"     Checked:     {summary['checked']}")
    print(f"     Active:      {summary['active']}")
    print(f"     Redirected:  {summary['redirected']}")
    print(f"     Dead:        {summary['dead']}")
    print(f"     Flagged:     {summary['flagged']} (seminal — manual review needed)")


def cmd_status(args):
    """Show run status."""
    db.init_db()
    run = db.get_run(args.run_id)
    if not run:
        print(f"Run not found: {args.run_id}")
        return
    print(f"\n{'='*60}")
    print(f"  Run Status: {args.run_id}")
    print(f"{'='*60}")
    print(f"  Problem:     {run['problem'][:70]}")
    print(f"  Status:      {run['status']}")
    print(f"  Created:     {run['created_at']}")
    print(f"  Break 0:     {'✓' if run['break0_done'] else '✗'}")
    print(f"  Break 1:     {'✓' if run['break1_done'] else '✗'}")
    print(f"  Break 2:     {'✓' if run['break2_done'] else '✗'}")
    print(f"  Completed:   {run.get('completed_at', '—')}")

    state = pipeline.get_state(args.run_id)
    print(f"  Progress:    {state['progress']['done']}/{state['progress']['total']} steps")
    if state["awaiting_break"] is not None:
        print(f"  Waiting on:  Break {state['awaiting_break']}")
    elif state["failed_steps"]:
        print(f"  Failed at:   {', '.join(state['failed_steps'])}")

    # Counts
    print(f"\n  Database entries:")
    for table, label in [
        ("sources",      "Sources"),
        ("gaps",         "Gaps"),
        ("implications", "Implications"),
        ("proposals",    "Proposals"),
        ("evaluations",  "Evaluations"),
        ("syntheses",    "Syntheses"),
        ("directions",   "Directions"),
        ("artifacts",    "Artifacts"),
    ]:
        n = db.count(table, {"run_id": args.run_id})
        print(f"    {label:<16} {n}")
    print()


def cmd_bank(args):
    """Show seminal bank proposals."""
    db.init_db()
    proposals = db.get_seminal_bank("pending_review")
    if not proposals:
        print("\n  No pending proposals in seminal bank.")
        return
    print(f"\n{'='*60}")
    print(f"  Seminal Bank — Pending Review ({len(proposals)} proposals)")
    print(f"{'='*60}")
    for p in proposals:
        print(f"\n  [{p['bank_id']}] {p['proposed_theme']}")
        print(f"  Reason:  {p.get('reason','')}")
        print(f"  Problem: {p.get('problem_origin','')[:60]}")
        print(f"  Date:    {p.get('date_proposed','')}")
    print()


def cmd_runs(args):
    """List recent runs."""
    db.init_db()
    runs = db.fetch("runs")
    runs.sort(key=lambda r: r.get("created_at",""), reverse=True)
    if not runs:
        print("\n  No runs found.")
        return
    print(f"\n{'='*60}")
    print(f"  Recent Runs")
    print(f"{'='*60}")
    for r in runs[:20]:
        breaks_done = sum([r.get("break0_done",0), r.get("break1_done",0), r.get("break2_done",0)])
        print(f"  {r['run_id']}  [{r['status']}]  breaks:{breaks_done}/3")
        print(f"    {r['problem'][:65]}")
    print()


def cmd_keys(args):
    """Show API key status."""
    from core.keys import print_key_status
    print_key_status()


def cmd_test(args):
    """Test a single source handler with a query and show the raw response."""
    source  = args.source
    query   = args.query

    from agents.social import SOURCE_HANDLERS
    from core.keys import print_key_status

    handler = SOURCE_HANDLERS.get(source)
    if not handler:
        print(f"\n  ERROR: Unknown source '{source}'")
        print(f"  Available: {', '.join(SOURCE_HANDLERS.keys())}")
        return

    print(f"\n{'='*60}")
    print(f"  Source Test — {source}")
    print(f"  Query:  {query}")
    print(f"{'='*60}\n")

    # Show key status for context
    print_key_status()

    print(f"  Running query...\n")
    try:
        results = handler.search(query, [], limit=3, run_id="TEST")
    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return

    if not results:
        print("  No results returned.")
        print()
        if source == "scopus":
            print("  Possible reasons:")
            print("  - Not on institutional IP/VPN")
            print("  - SCOPUS_API_KEY not set in .env")
            print("  - API key not yet activated by Elsevier")
        return

    print(f"  {len(results)} result(s) returned\n")
    print(f"{'─'*60}")

    for i, r in enumerate(results, 1):
        print(f"\n  [{i}] {r.get('title','(no title)')}")
        print(f"       Authors:  {', '.join(r.get('authors', [])[:3]) or '(none)'}")
        print(f"       Year:     {r.get('year', '?')}")
        print(f"       Journal:  {r.get('journal', r.get('source_name',''))}")
        print(f"       DOI:      {r.get('doi', '(none)')}")
        print(f"       Link:     {r.get('active_link', '(none)')}")
        if r.get('cited_by') is not None:
            print(f"       Cited by: {r['cited_by']}")
        abstract = r.get('abstract', '')
        if abstract:
            print(f"       Abstract: {abstract[:300]}{'...' if len(abstract) > 300 else ''}")
        else:
            print(f"       Abstract: (empty — check IP/VPN if using Scopus)")

    print(f"\n{'='*60}")
    if source == "scopus" and results:
        has_abstracts = any(r.get('abstract') for r in results)
        if has_abstracts:
            print("  ✅ Abstracts present — institutional access confirmed")
        else:
            print("  ⚠️  No abstracts — you may need to connect to VPN")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-Agent Research Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  run       Run the pipeline on a problem
  collect   Run Social passive collection (twice weekly)
  recheck   Run link health check
  status    Show status of a run
  bank      Show seminal bank proposals pending review
  runs      List recent runs
        """
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run the pipeline")
    p_run.add_argument("--problem", default=None,
                       help="Research problem statement (required for a new run)")
    p_run.add_argument("--run-id",  default=None,  help="Resume an existing run")
    p_run.add_argument("--resume",  action="store_true", help="Resume from last completed step")
    p_run.set_defaults(func=cmd_run)

    # steps
    p_steps = sub.add_parser("steps", help="Show per-step progress for a run")
    p_steps.add_argument("--run-id", required=True)
    p_steps.set_defaults(func=cmd_steps)

    # rerun
    p_rerun = sub.add_parser("rerun", help="Re-run a step and everything after it")
    p_rerun.add_argument("--run-id", required=True)
    p_rerun.add_argument("--step",   required=True,
                         help=f"One of: {', '.join(s.name for s in pipeline.STEP_DEFS)}")
    p_rerun.add_argument("--only",   action="store_true",
                         help="Reset just this step, leaving later steps alone")
    p_rerun.add_argument("--yes",    action="store_true", help="Skip the confirmation")
    p_rerun.set_defaults(func=cmd_rerun)

    # collect
    p_collect = sub.add_parser("collect", help="Social passive collection")
    p_collect.set_defaults(func=cmd_collect)

    # recheck
    p_recheck = sub.add_parser("recheck", help="Link health check")
    p_recheck.set_defaults(func=cmd_recheck)

    # status
    p_status = sub.add_parser("status", help="Show run status")
    p_status.add_argument("--run-id", required=True, help="Run ID to check")
    p_status.set_defaults(func=cmd_status)

    # bank
    p_bank = sub.add_parser("bank", help="Show seminal bank proposals")
    p_bank.set_defaults(func=cmd_bank)

    # keys
    p_keys = sub.add_parser("keys", help="Show API key status")
    p_keys.set_defaults(func=cmd_keys)

    # test
    p_test = sub.add_parser("test", help="Test a single source handler")
    p_test.add_argument("--source", required=True,
                        help="Source to test (e.g. scopus, openalex, arxiv)")
    p_test.add_argument("--query",  required=True,
                        help="Search query to run")
    p_test.set_defaults(func=cmd_test)

    # runs
    p_runs = sub.add_parser("runs", help="List recent runs")
    p_runs.set_defaults(func=cmd_runs)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
