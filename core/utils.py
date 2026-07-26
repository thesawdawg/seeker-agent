"""
Shared utilities
----------------
Logging setup, ID generation, config loading.
"""

import uuid
import json
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime

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
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"RUN-{ts}-{uuid.uuid4().hex[:4].upper()}"


def load_config() -> dict:
    """Load config.json — theme bank and source stack."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found at {CONFIG_PATH}. "
            f"Please create it with your theme bank and source configuration."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


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
