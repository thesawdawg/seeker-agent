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
JS   = STATIC / "app.js"


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
    js = JS.read_text()
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
    assert not missing, f"app.js looks up ids nothing defines: {sorted(missing)}"


def test_every_api_path_has_a_route():
    """The frontend and the API must not drift apart."""
    js = JS.read_text()
    app = (Path(__file__).parent.parent / "web" / "app.py").read_text()

    called = set()
    for raw in re.findall(r"api\(\s*[`'\"]([^`'\"]+)[`'\"]", js):
        called.add(re.sub(r"\$\{[^}]+\}", "{p}", raw.split("?")[0]))

    routes = {re.sub(r"\{[^}]+\}", "{p}", path) for _, path in
              re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"\)', app)}

    missing = sorted(called - routes)
    assert not missing, f"app.js calls paths with no route: {missing}"


def test_modal_can_be_dismissed():
    """The modal needs a cancel path wired, not just a confirm one."""
    js = JS.read_text()
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
