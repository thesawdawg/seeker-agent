"""
Shared utilities
----------------
Logging setup, ID generation, config loading.
"""

import uuid
import json
import logging
import logging.handlers
import threading
from pathlib import Path
from datetime import datetime, timezone

LOGS_DIR  = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def setup_logging(run_id: str = "system") -> logging.Logger:
    """Configure logging — file + console."""
    log_file = LOGS_DIR / f"{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(log_file))
        ]
    )
    return logging.getLogger("pipeline")


def generate_id(prefix: str) -> str:
    """Generate a short unique ID with prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def generate_run_id() -> str:
    # datetime.utcnow() is deprecated and slated for removal; everywhere else
    # in the codebase already uses an aware UTC now (review C8).
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"RUN-{ts}-{uuid.uuid4().hex[:4].upper()}"


_config_cache: tuple = None       # ((path, mtime_ns, size), raw_text)
_config_lock = threading.Lock()


def load_config() -> dict:
    """
    Load config.json — theme bank and source stack.

    The file's *text* is cached, keyed on (path, mtime, size), and parsed per
    call. Two reasons it is done that way round:

    - Callers mutate the dict they are handed, so they each need their own.
      Caching the parsed object and deep-copying it is the obvious
      alternative and is measurably *slower* than simply parsing again —
      deepcopy of this structure costs more than json.loads does.
    - Keying on mtime is what lets the worker pick up an operator's edit
      without a restart (review C7). save_config goes through os.replace, so
      the mtime always moves.

    The saving is the file read, not the parse (review O4). Modest, but it is
    on request paths that run several times per page load.
    """
    global _config_cache
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found at {CONFIG_PATH}. "
            f"Please create it with your theme bank and source configuration."
        )
    stat = CONFIG_PATH.stat()
    # The path is part of the key: CONFIG_PATH is redirected in tests, and a
    # copy that preserves mtime would otherwise look identical to the original.
    stamp = (str(CONFIG_PATH), stat.st_mtime_ns, stat.st_size)
    with _config_lock:
        cached = _config_cache
    if cached and cached[0] == stamp:
        return json.loads(cached[1])

    raw = CONFIG_PATH.read_text()
    with _config_lock:
        _config_cache = (stamp, raw)
    return json.loads(raw)


def invalidate_config_cache() -> None:
    """Drop the cached config — for tests and for an explicit reload."""
    global _config_cache
    with _config_lock:
        _config_cache = None


def save_config(config: dict) -> None:
    """
    Atomically write config.json (F12).

    Writes to a temp file first, then renames — so a crash mid-write
    never leaves a half-written config. The write is also validated as
    JSON before the rename, so a malformed config never reaches disk.
    """
    import os
    import tempfile
    # Validate: must be JSON-serializable
    serialized = json.dumps(config, indent=2, ensure_ascii=False)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same directory (so rename is atomic)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(CONFIG_PATH.parent), suffix=".json.tmp",
        prefix="config_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.write("\n")
        os.replace(tmp_path, str(CONFIG_PATH))
        invalidate_config_cache()
    except Exception:
        # Clean up the temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_themes(config: dict) -> list[dict]:
    return config.get("themes", [])


def get_source_config(config: dict, source_id: str) -> dict:
    return config.get("sources", {}).get(source_id, {})


def match_themes_to_problem(problem: str, themes: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Simple keyword-based theme matching.
    Returns (selected_themes, excluded_themes).
    
    Each theme has keywords — if any keyword seed appears in the problem,
    the theme is selected.
    """
    problem_lower = problem.lower()
    selected = []
    excluded = []

    for theme in themes:
        keywords = theme.get("keywords", [])
        matched = False
        for kw in keywords:
            seed = kw.get("seed", "").lower()
            if seed and seed in problem_lower:
                matched = True
                break
            # Also check theme label and id
            if theme.get("label", "").lower() in problem_lower:
                matched = True
                break
            if theme.get("theme_id", "").lower() in problem_lower:
                matched = True
                break

        if matched:
            selected.append(theme)
        else:
            excluded.append({**theme, "reason": "No keyword match found in problem statement"})

    # If nothing matched, select all themes (better to be broad than miss)
    if not selected:
        selected = themes
        excluded = []

    return selected, excluded


def truncate_words(text: str, limit: int = 80) -> str:
    """
    Shorten `text` to at most `limit` characters without splitting a word.

    Plain slicing produced gap titles that broke mid-token and stranded
    punctuation — e.g. "...moral reasoning in D&" from "D&D", rendered inside
    surrounding quotes so the result read as an unclosed quotation. This backs
    up to the last whitespace boundary and appends an ellipsis instead.

    Returns `text` unchanged when it already fits.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    # Only honour the boundary if it leaves something readable; a single very
    # long token still has to be cut somewhere.
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + "…"
