"""
User-scoped preferences: defaults, appearance, and display options.

Storage reuses the `user_preferences` table that already backs the MCP
connection toggles (`core/users.py`) — a (user_id, pref_key, pref_value) store
with dotted keys. No new table is needed; this module supplies the schema that
the store itself has no opinion about.

`pref_value` is TEXT, so every key declares a type and the coercion runs in one
place rather than ad hoc at each call site (the MCP toggles improvise this today
with "1"/"0" strings).

Keys under the `mcp.` prefix belong to `core.users` and are deliberately not
exposed here — they have their own endpoints.
"""

import json
import logging

from core import users

logger = logging.getLogger(__name__)

# Reserved prefixes owned by other modules; never returned or accepted here.
_RESERVED_PREFIXES = ("mcp.",)


def _coerce_int(raw, spec):
    value = int(raw)
    if "min" in spec and value < spec["min"]:
        raise ValueError(f"must be at least {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ValueError(f"must be at most {spec['max']}")
    return value


def _coerce_float(raw, spec):
    value = float(raw)
    if "min" in spec and value < spec["min"]:
        raise ValueError(f"must be at least {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ValueError(f"must be at most {spec['max']}")
    return value


def _coerce_enum(raw, spec):
    value = str(raw)
    if value not in spec["choices"]:
        raise ValueError(f"must be one of: {', '.join(spec['choices'])}")
    return value


def _coerce_bool(raw, _spec):
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _coerce_list(raw, _spec):
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (TypeError, ValueError):
            pass
        return [p.strip() for p in raw.split(",") if p.strip()]
    raise ValueError("must be a list")


def _coerce_str(raw, _spec):
    return str(raw)


_COERCERS = {
    "bool":  _coerce_bool,
    "int":   _coerce_int,
    "float": _coerce_float,
    "enum":  _coerce_enum,
    "list":  _coerce_list,
    "str":   _coerce_str,
}


# ── Schema ───────────────────────────────────────────────────────────────
#
# group  — which tab section the key renders under
# label  — the control's visible label
# help   — optional one-line explanation
#
# Adding a key here is all that's needed; the API and the settings UI are both
# schema-driven and pick it up without further changes.

SCHEMA = {
    # C1 — run defaults. These are what the New Run form pre-fills from, so a
    # returning researcher stops re-making the same choices every run.
    "defaults.provider": {
        "type": "str", "default": "", "group": "run_defaults",
        "label": "Default provider",
        "help": "Pre-selected on the New Run form. Blank uses your first connected provider.",
    },
    "defaults.model_primary": {
        "type": "str", "default": "", "group": "run_defaults",
        "label": "Default primary model",
        "help": "Heavy reasoning agents. Blank uses the provider's saved primary.",
    },
    "defaults.model_light": {
        "type": "str", "default": "", "group": "run_defaults",
        "label": "Default light model",
        "help": "Social and Scribe. Blank uses the provider's saved light model.",
    },
    "defaults.sources": {
        "type": "list", "default": [], "group": "run_defaults",
        "label": "Default sources",
        "help": "Pre-checked on New Run. Empty means the config default set.",
    },
    "defaults.template": {
        "type": "str", "default": "", "group": "run_defaults",
        "label": "Default template",
        "help": "Auto-applied when you open the New Run form.",
    },
    "defaults.results_per_source": {
        "type": "int", "default": 8, "min": 1, "max": 25,
        "group": "run_defaults",
        "label": "Results per source",
        "help": "Directly drives run cost and duration.",
    },

    # C3 — appearance.
    "appearance.theme": {
        "type": "enum", "default": "system",
        "choices": ["system", "light", "dark"],
        "group": "appearance", "label": "Theme",
        "help": "Follows your account rather than one browser.",
    },
    "appearance.density": {
        "type": "enum", "default": "comfortable",
        "choices": ["comfortable", "compact"],
        "group": "appearance", "label": "Density",
    },
    "appearance.font_scale": {
        "type": "float", "default": 1.0, "min": 0.9, "max": 1.3,
        "group": "appearance", "label": "Font scale",
    },

    # C5 — display preferences that are currently hardcoded.
    "display.runs_per_page": {
        "type": "int", "default": 25, "min": 10, "max": 100,
        "group": "display", "label": "Runs per page",
    },
    "display.runs_sort": {
        "type": "enum", "default": "recent",
        "choices": ["recent", "oldest", "problem"],
        "group": "display", "label": "Default run sort",
    },
    "display.default_run_tab": {
        "type": "enum", "default": "overview",
        "choices": ["overview", "sources", "tree", "artifacts"],
        "group": "display", "label": "Default run tab",
        "help": "A run waiting at a break always opens on the break regardless.",
    },
    "display.timestamp_format": {
        "type": "enum", "default": "relative",
        "choices": ["relative", "absolute"],
        "group": "display", "label": "Timestamps",
    },
    "display.show_run_ids": {
        "type": "bool", "default": True,
        "group": "display", "label": "Show run IDs",
    },

    # C2 — notifications. Browser-side only; there is no SMTP seam yet, so
    # email is deliberately absent rather than present and broken.
    "notify.browser": {
        "type": "bool", "default": False, "group": "notifications",
        "label": "Browser notifications",
        "help": "Requires granting permission in your browser.",
    },
    "notify.title_badge": {
        "type": "bool", "default": True, "group": "notifications",
        "label": "Unread badge in the tab title",
    },
    "notify.on_break_ready": {
        "type": "bool", "default": True, "group": "notifications",
        "label": "Notify when a break is ready",
    },
    "notify.on_run_complete": {
        "type": "bool", "default": True, "group": "notifications",
        "label": "Notify when a run completes",
    },
    "notify.on_run_failed": {
        "type": "bool", "default": True, "group": "notifications",
        "label": "Notify when a run fails",
    },
}

GROUP_LABELS = {
    "run_defaults":  "Run defaults",
    "appearance":    "Appearance",
    "display":       "Display",
    "notifications": "Notifications",
}


def defaults() -> dict:
    """The full default settings map."""
    return {key: spec["default"] for key, spec in SCHEMA.items()}


def _decode(key: str, raw: str):
    """Turn a stored TEXT value back into its declared type."""
    spec = SCHEMA[key]
    try:
        return _COERCERS[spec["type"]](raw, spec)
    except (TypeError, ValueError) as e:
        # A bad stored value must not break the whole settings response.
        logger.warning(f"[settings] Ignoring unreadable value for '{key}': {e}")
        return spec["default"]


def _encode(value) -> str:
    """Serialize a typed value for TEXT storage."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def get_settings(user_id: str) -> dict:
    """Stored settings merged over the defaults. Always returns every key."""
    merged = defaults()
    stored = users.get_all_preferences(user_id)
    for key, raw in stored.items():
        if key in SCHEMA:
            merged[key] = _decode(key, raw)
    return merged


def update_settings(user_id: str, incoming: dict) -> dict:
    """
    Apply a partial update.

    Validates every key before writing any of them, so a request with one bad
    value does not leave the account half-updated. Raises ValueError with a
    message naming the offending key.
    """
    if not isinstance(incoming, dict):
        raise ValueError("Body must be an object of {key: value}")

    validated = {}
    for key, value in incoming.items():
        if key.startswith(_RESERVED_PREFIXES):
            raise ValueError(f"'{key}' is managed elsewhere and cannot be set here")
        spec = SCHEMA.get(key)
        if not spec:
            raise ValueError(f"Unknown setting '{key}'")
        try:
            validated[key] = _COERCERS[spec["type"]](value, spec)
        except (TypeError, ValueError) as e:
            raise ValueError(f"'{key}': {e}")

    for key, value in validated.items():
        users.set_preference(user_id, key, _encode(value))

    return get_settings(user_id)


def schema_for_ui() -> list:
    """
    The schema as an ordered list of groups, for a UI that renders itself.

    Keeps default values out of the frontend entirely — it asks for the shape
    and the current values, and never hardcodes either.
    """
    groups = {}
    for key, spec in SCHEMA.items():
        group = spec["group"]
        groups.setdefault(group, {
            "id": group,
            "label": GROUP_LABELS.get(group, group),
            "settings": [],
        })
        entry = {
            "key":     key,
            "type":    spec["type"],
            "label":   spec["label"],
            "default": spec["default"],
        }
        for optional in ("help", "choices", "min", "max"):
            if optional in spec:
                entry[optional] = spec[optional]
        groups[group]["settings"].append(entry)
    return [groups[g] for g in GROUP_LABELS if g in groups]
