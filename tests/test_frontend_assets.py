"""
Frontend static checks.

These catch the failure modes that are invisible without rendering a page, and
which the API and logic tests cannot reach.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

STATIC = Path(__file__).parent.parent / "web" / "static"
HTML = STATIC / "index.html"
CSS  = STATIC / "style.css"


def frontend_module_files():
    """Return every application module reachable from the HTML entry point."""
    html = HTML.read_text()
    entries = re.findall(
        r'<script type="module" src="/static/([^"?]+\.js)(?:\?[^"]*)?"></script>',
        html,
    )
    pending = [STATIC / entry for entry in entries]
    found = []
    seen = set()
    import_pattern = re.compile(
        r"(?:from\s+|import\s*)['\"]([^'\"]+\.js)(?:\?[^'\"]*)?['\"]"
    )
    while pending:
        module = pending.pop()
        module = module.resolve()
        if module in seen:
            continue
        seen.add(module)
        found.append(module)
        source = module.read_text()
        for specifier in import_pattern.findall(source):
            if specifier.startswith("."):
                pending.append(module.parent / specifier)
    return found


def frontend_javascript():
    return "\n".join(module.read_text() for module in frontend_module_files())


def test_frontend_uses_one_native_module_entry_point():
    html = HTML.read_text()
    entries = re.findall(
        r'<script type="module" src="/static/([^"?]+\.js)(?:\?[^"]*)?"></script>',
        html,
    )
    assert entries == ["js/main.js"]
    assert len(frontend_module_files()) >= 15


def test_core_and_components_do_not_import_features():
    for module in frontend_module_files():
        relative = module.relative_to(STATIC).as_posix()
        if relative.startswith(("js/core/", "js/components/")):
            assert "/features/" not in module.read_text(), (
                f"{relative} reverses the dependency direction"
            )


def test_feature_state_changes_use_the_store():
    assignment = re.compile(r"\bstate\.[A-Za-z_$][A-Za-z0-9_$]*\s*=(?!=)")
    offenders = {}
    for module in frontend_module_files():
        relative = module.relative_to(STATIC).as_posix()
        if not relative.startswith("js/features/"):
            continue
        matches = assignment.findall(module.read_text())
        if matches:
            offenders[relative] = matches
    assert not offenders, f"direct feature state assignments: {offenders}"


def test_hidden_attribute_is_enforced():
    """
    Regression: the modal was painted on every page load and could not be
    dismissed.

    `.modal-backdrop { display: flex }` beat the UA stylesheet's
    `[hidden] { display: none }`, so the element ignored its hidden attribute
    and setting `.hidden = true` from JavaScript did nothing. #view-login and
    #topbar had the same collision.
    """
    css = CSS.read_text()
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
        "style.css must force [hidden] { display: none !important }, or any "
        "class that sets display will override the hidden attribute"
    )


def test_every_hidden_element_is_covered():
    """
    Any element that both carries `hidden` and matches a rule setting `display`
    depends on the guard above. This lists them so the coupling is explicit.
    """
    html = HTML.read_text()
    css = CSS.read_text()

    display_classes = set()
    for selector, block in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if not re.search(r"(^|;)\s*display:", block):
            continue
        for cls in re.findall(r"\.([a-zA-Z0-9_-]+)", selector):
            display_classes.add(cls)

    colliding = []
    for tag in re.findall(r"<[a-z]+[^>]*\bhidden\b[^>]*>", html):
        classes = re.search(r'class="([^"]*)"', tag)
        ident = re.search(r'id="([^"]*)"', tag)
        if not classes:
            continue
        overlap = set(classes.group(1).split()) & display_classes
        if overlap:
            colliding.append((ident.group(1) if ident else "?", sorted(overlap)))

    # Not a failure — the guard handles them. Asserting the guard exists is the
    # real check; this documents which elements would break without it.
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
                     css), f"elements relying on the guard: {colliding}"


def test_no_external_resources():
    """The stack must run offline and behind a firewall — nothing off-host."""
    html = HTML.read_text()
    css = CSS.read_text()

    for name, text in (("index.html", html), ("style.css", css)):
        remote = re.findall(r'(?:src|href)="(https?://[^"]+)"', text)
        assert not remote, f"{name} references external resources: {remote}"
        assert "@import" not in text, f"{name} uses @import"

    # data: URIs are fine (the favicon is an inline SVG)
    assert "cdn" not in html.lower() or "data:" in html


def test_every_js_element_id_exists():
    """A typo in a selector is silent at runtime — $() just returns null."""
    js = frontend_javascript()
    html = HTML.read_text()

    looked_up = set(re.findall(r"\$\('#([a-zA-Z0-9_-]+)'", js))
    defined = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    created = set(re.findall(r"id:\s*'([a-zA-Z0-9_-]+)'", js))

    # Ids built from template literals — `role-${role}` covers role-primary
    patterns = [
        re.compile("^" + re.sub(r"\\\$\\\{[^}]*\\\}", ".+", re.escape(tpl)) + "$")
        for tpl in re.findall(r"id:\s*`([^`]+)`", js)
    ]

    missing = {
        name for name in looked_up - defined - created
        if not any(p.match(name) for p in patterns)
    }
    assert not missing, f"frontend scripts look up ids nothing defines: {sorted(missing)}"


def test_every_api_path_has_a_route():
    """The frontend and the API must not drift apart."""
    js = frontend_javascript()
    app_py = (Path(__file__).parent.parent / "web" / "app.py").read_text()

    called = set()
    for raw in re.findall(r"api\(\s*[`'\"]([^`'\"]+)[`'\"]", js):
        called.add(re.sub(r"\$\{[^}]+\}", "{p}", raw.split("?")[0]))

    # Routes declared directly on the app via @app.get/post/...
    routes = {re.sub(r"\{[^}]+\}", "{p}", path) for _, path in
              re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"\)', app_py)}

    # Routes registered via APIRouter in auth backends (included via
    # app.include_router). Scan the auth package for @router.(get|post/...)
    auth_dir = Path(__file__).parent.parent / "web" / "auth"
    if auth_dir.is_dir():
        for py in auth_dir.rglob("*.py"):
            src = py.read_text()
            routes.update(
                re.sub(r"\{[^}]+\}", "{p}", path)
                for _, path in re.findall(
                    r'@router\.(get|post|put|patch|delete)\("([^"]+)"\)', src)
            )

    missing = sorted(called - routes)
    assert not missing, f"frontend scripts call paths with no route: {missing}"


def test_password_login_does_not_wait_for_model_provider():
    """A down model provider must not delay username/password sign-in."""
    auth_js = (STATIC / "js" / "features" / "auth.js").read_text()
    branch = re.search(
        r"if \(me\.auth_kind === 'password'\) \{(?P<password>.*?)"
        r"\}\s*else\s*\{(?P<other>.*?)\}",
        auth_js,
        re.DOTALL,
    )
    assert branch, "afterSignIn must distinguish password-authenticated users"
    assert "void loadModels()" in branch.group("password")
    assert "await loadModels()" not in branch.group("password")
    assert "await loadModels()" in branch.group("other")


def test_provider_settings_show_live_health_indicators():
    settings_js = (STATIC / "js" / "features" / "settings.js").read_text()
    css = CSS.read_text()

    assert "api('/api/providers/health')" in settings_js
    assert "provider-health is-checking" in settings_js
    assert "role: 'status'" in settings_js
    for status_name in ("is-checking", "is-ok", "is-rejected", "is-unavailable"):
        assert f".provider-health.{status_name}" in css


def test_modal_can_be_dismissed():
    """The modal needs a cancel path wired, not just a confirm one."""
    js = frontend_javascript()
    assert "modal-cancel" in js, "no cancel handler for the modal"
    assert re.search(r"key\s*===\s*'Escape'", js), "Escape does not close the modal"


@pytest.mark.parametrize("view", ["login", "runs", "new", "run", "admin", "guide"])
def test_views_are_declared_hidden_except_the_first(view):
    """
    Only one view may be visible at load. Everything else starts hidden, and
    showView() flips them.
    """
    html = HTML.read_text()
    match = re.search(rf'<section[^>]*id="view-{view}"[^>]*>', html)
    assert match, f"view-{view} is missing"
    if view != "login":
        assert "hidden" in match.group(0), f"view-{view} must start hidden"


# ---------------------------------------------------------------------------
# Accessibility (review X4)
#
# The interface is keyboard-driven and long-running: a researcher watches it
# for twenty minutes at a stretch. These pin the parts that make that usable
# without a mouse or without sight, all of which were missing.
# ---------------------------------------------------------------------------

def test_tabs_carry_tablist_semantics():
    """
    The tab strips are real buttons, so they were always focusable — but they
    announced as five unrelated buttons, with no indication of which was
    current, because aria-selected is what conveys that, not a CSS class.
    """
    html = HTML.read_text()
    assert html.count('role="tablist"') >= 2, "both tab strips need a tablist role"
    assert html.count('role="tab"') >= 9
    assert html.count('role="tabpanel"') >= 9
    assert 'aria-selected="true"' in html
    assert 'aria-controls="panel-overview"' in html


def test_tab_selection_updates_aria_not_only_the_class():
    js = frontend_javascript()
    assert "markSelectedTab" in js
    assert "aria-selected" in js, (
        "switching tabs must update aria-selected; a screen reader reads that, "
        "not the is-active class"
    )


def test_tablists_support_arrow_key_navigation():
    """Expected of anything using the tab role, and the reason for the
    roving tabindex."""
    js = frontend_javascript()
    assert "wireTablistKeys" in js
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in js


def test_status_changes_are_announced():
    """
    Toasts carry "your session expired"; the live card carries run progress.
    Without a live region both are silent to a screen reader — and progress
    is the single thing a blind user most needs during a long run.
    """
    html = HTML.read_text()
    js = frontend_javascript()
    assert re.search(r'id="toasts"[^>]*aria-live', html), \
        "the toast stack must be a live region"
    assert re.search(r"id: 'live-card'[^)]*aria-live", js), \
        "the live progress card must be a live region"


def test_keyboard_focus_is_visible_beyond_form_fields():
    css = CSS.read_text()
    assert ":focus-visible" in css
    for selector in ("button:focus-visible", "[role=\"tab\"]:focus-visible"):
        assert selector in css, f"{selector} needs a visible focus ring"


def test_reduced_motion_is_respected():
    """The spinner and the pulsing live dot run for the length of a run."""
    css = CSS.read_text()
    assert "prefers-reduced-motion" in css
    block = css.split("prefers-reduced-motion", 1)[1]
    assert "animation-duration" in block and "transition-duration" in block


def test_a_div_with_a_button_role_responds_to_the_keyboard():
    """A real button fires on Enter and Space; one faked with a role must too."""
    html = HTML.read_text()
    js = frontend_javascript()
    if 'role="button"' not in html:
        pytest.skip("no faked buttons in the markup")
    assert "'Enter'" in js and "' '" in js
    assert "getAttribute('role') === 'button'" in js


def test_the_estimate_card_is_announced_when_it_updates():
    """It changes as the user toggles sources; a silent change is invisible."""
    html = HTML.read_text()
    assert re.search(r'id="new-estimate"[^>]*aria-live', html)
