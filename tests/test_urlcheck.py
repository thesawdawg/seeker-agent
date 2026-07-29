"""
Tests for URL validation pre-check in break reports.

Covers:
  - core.urlcheck: format validation (is_well_formed) and reachability
    (is_reachable) — the latter mocked, no real network.
  - core.breaks._validate_source_for_display: the integration point that
    filters invalid URLs from source dicts before they reach the markdown
    or web UI renderer.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.urlcheck import is_well_formed, is_reachable, validate_url
from core.breaks import _validate_source_for_display, _format_full_reference


# ---------------------------------------------------------------------------
# is_well_formed
# ---------------------------------------------------------------------------

class TestIsWellFormed:
    def test_valid_https(self):
        assert is_well_formed("https://doi.org/10.1000/xyz123")

    def test_valid_http(self):
        assert is_well_formed("http://arxiv.org/abs/2401.00001")

    def test_empty_string(self):
        assert not is_well_formed("")

    def test_none(self):
        assert not is_well_formed(None)

    def test_whitespace_only(self):
        assert not is_well_formed("   ")

    def test_no_scheme(self):
        assert not is_well_formed("doi.org/10.1000/xyz")

    def test_wrong_scheme(self):
        assert not is_well_formed("ftp://example.com/file")

    def test_no_dot_in_host(self):
        assert not is_well_formed("http://localhost-thing/path")
        # Actually localhost is allowed
        assert is_well_formed("http://localhost:3000/api")

    def test_placeholder_url_if_known(self):
        assert not is_well_formed("url if known")

    def test_placeholder_na(self):
        assert not is_well_formed("n/a")

    def test_placeholder_none(self):
        assert not is_well_formed("none")

    def test_placeholder_todo(self):
        assert not is_well_formed("todo")

    def test_template_braces(self):
        assert not is_well_formed("https://example.com/{doi}")

    def test_angle_brackets(self):
        assert not is_well_formed("<https://example.com>")

    def test_example_com(self):
        assert not is_well_formed("https://example.com/page")

    def test_bare_doi(self):
        """A bare DOI like '10.1000/xyz' is not a URL."""
        assert not is_well_formed("10.1000/xyz123")


# ---------------------------------------------------------------------------
# is_reachable (mocked — no real network in tests)
# ---------------------------------------------------------------------------

class TestIsReachable:
    def test_malformed_url_returns_false_without_request(self):
        assert not is_reachable("not a url")

    def test_empty_url(self):
        assert not is_reachable("")

    @patch("core.urlcheck.requests")
    def test_200_returns_true(self, mock_requests):
        resp = MagicMock(status_code=200)
        mock_requests.head.return_value = resp
        assert is_reachable("https://doi.org/10.1000/xyz")

    @patch("core.urlcheck.requests")
    def test_404_returns_false(self, mock_requests):
        resp = MagicMock(status_code=404)
        mock_requests.head.return_value = resp
        assert not is_reachable("https://sciencedirect.com/gone")

    @patch("core.urlcheck.requests")
    def test_410_returns_false(self, mock_requests):
        resp = MagicMock(status_code=410)
        mock_requests.head.return_value = resp
        assert not is_reachable("https://sciencedirect.com/gone")

    @patch("core.urlcheck.requests")
    def test_network_error_returns_false(self, mock_requests):
        """A transient network error means reachability could not be verified."""
        mock_requests.head.side_effect = TimeoutError("timeout")
        assert not is_reachable("https://sciencedirect.com/page")


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------

class TestValidateUrl:
    def test_format_only_default(self):
        assert validate_url("https://doi.org/10.1000/xyz")

    def test_format_only_rejects_malformed(self):
        assert not validate_url("n/a")

    @patch("core.urlcheck.requests")
    def test_with_reachability_check(self, mock_requests):
        mock_requests.head.return_value = MagicMock(status_code=404)
        assert not validate_url("https://sciencedirect.com/gone", check_reachable=True)


# ---------------------------------------------------------------------------
# _validate_source_for_display
# ---------------------------------------------------------------------------

class TestValidateSourceForDisplay:
    """Tests for provider_api sources — URLs are eligible for display."""

    def test_valid_source_passes_through(self):
        s = {
            "source_id": "SEM-001",
            "doi": "10.1000/xyz123",
            "active_link": "https://arxiv.org/abs/2401.00001",
            "link_status": "active",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == "10.1000/xyz123"
        assert result["active_link"] == "https://arxiv.org/abs/2401.00001"
        assert result["_url_issues"] == []

    def test_malformed_active_link_removed(self):
        s = {
            "source_id": "SEM-002",
            "doi": "",
            "active_link": "url if known",
            "link_status": "active",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert result["active_link"] == ""
        assert len(result["_url_issues"]) == 1
        assert "malformed" in result["_url_issues"][0]

    def test_dead_link_status_removed(self):
        s = {
            "source_id": "SEM-003",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/dead",
            "link_status": "dead",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""
        assert len(result["_url_issues"]) == 2  # DOI + active_link

    def test_unreachable_status_kept(self):
        """unreachable is a transient network issue, not an invalid URL."""
        s = {
            "source_id": "SEM-004",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "unreachable",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == "10.1000/xyz"
        assert result["active_link"] == "https://sciencedirect.com/page"
        assert result["_url_issues"] == []

    def test_unchecked_does_live_check(self):
        """When link_status is unchecked, a live HEAD check is performed."""
        s = {
            "source_id": "SEM-005",
            "doi": "",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "unchecked",
            "url_origin": "provider_api",
        }
        with patch("core.breaks.is_reachable", return_value=False) as mock_check:
            result = _validate_source_for_display(s)
            assert result["active_link"] == ""
            assert "dead link" in result["_url_issues"][0]
            mock_check.assert_called_once()

    def test_unchecked_live_check_passes(self):
        s = {
            "source_id": "SEM-006",
            "doi": "10.1000/xyz",
            "active_link": "",
            "link_status": "unchecked",
            "url_origin": "provider_api",
        }
        with patch("core.breaks.is_reachable", return_value=True):
            result = _validate_source_for_display(s)
            assert result["doi"] == "10.1000/xyz"
            assert result["_url_issues"] == []

    def test_missing_link_status_does_live_check(self):
        s = {
            "source_id": "SEM-007",
            "doi": "",
            "active_link": "https://sciencedirect.com/page",
            "url_origin": "provider_api",
            # no link_status key at all
        }
        with patch("core.breaks.is_reachable", return_value=False):
            result = _validate_source_for_display(s)
            assert result["active_link"] == ""

    def test_does_not_mutate_original(self):
        s = {
            "source_id": "SEM-008",
            "doi": "10.1000/xyz",
            "active_link": "url if known",
            "link_status": "active",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert s["active_link"] == "url if known"  # original unchanged
        assert result["active_link"] == ""          # copy is filtered

    def test_all_urls_malformed(self):
        """All three URL fields are garbage — all removed, all reported."""
        s = {
            "source_id": "SEM-009",
            "doi": "http://",  # no host → malformed
            "active_link": "n/a",
            "catalog_url": "todo",
            "link_status": "active",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""
        assert result["catalog_url"] == ""
        assert len(result["_url_issues"]) == 3


# ---------------------------------------------------------------------------
# _validate_source_for_display — provenance gate (llm_synthesis sources)
# ---------------------------------------------------------------------------

class TestValidateSourceForDisplayProvenance:
    """LLM-synthesized sources (Grounder, Historian) must never show their
    active_link or doi in displayed content. The URLs come from LLM output,
    not provider API responses, so they may be hallucinated or mangled.

    catalog_url is exempt — it is only ever written by the Librarian from
    the Primo library catalog API."""

    def test_llm_synthesis_active_link_hidden(self):
        """Even an 'active' link from llm_synthesis is hidden."""
        s = {
            "source_id": "HIST-001",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "active",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""
        assert len(result["_url_issues"]) == 2
        assert all("not from provider API" in i for i in result["_url_issues"])

    def test_llm_synthesis_redirected_link_hidden(self):
        s = {
            "source_id": "HIST-002",
            "doi": "",
            "active_link": "https://doi.org/10.1000/redirected",
            "link_status": "redirected",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert result["active_link"] == ""
        assert "not from provider API" in result["_url_issues"][0]

    def test_llm_synthesis_unreachable_link_hidden(self):
        s = {
            "source_id": "HIST-003",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "unreachable",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""

    def test_llm_synthesis_dead_link_hidden(self):
        s = {
            "source_id": "HIST-004",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/dead",
            "link_status": "dead",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""

    def test_llm_synthesis_malformed_link_hidden(self):
        s = {
            "source_id": "HIST-005",
            "doi": "",
            "active_link": "url if known",
            "link_status": "active",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert result["active_link"] == ""
        # Provenance gate fires before malformed check
        assert "not from provider API" in result["_url_issues"][0]

    def test_llm_synthesis_catalog_url_kept(self):
        """catalog_url is from the Librarian API, not LLM synthesis."""
        s = {
            "source_id": "HIST-006",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "catalog_url": "https://primo.library.edu/record/123",
            "link_status": "active",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""           # hidden by provenance gate
        assert result["active_link"] == ""   # hidden by provenance gate
        assert result["catalog_url"] == "https://primo.library.edu/record/123"

    def test_missing_url_origin_treated_as_llm(self):
        """Legacy rows without url_origin default to hiding (safe default)."""
        s = {
            "source_id": "SEM-LEGACY",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "active",
            # no url_origin key
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""
        assert "missing" in result["_url_issues"][0]

    def test_grounder_seminal_links_hidden(self):
        """Grounder's seminal works have url_origin=llm_synthesis."""
        s = {
            "source_id": "SEM-001",
            "doi": "10.1000/xyz",
            "active_link": "https://arxiv.org/abs/2401.00001",
            "link_status": "active",
            "url_origin": "llm_synthesis",
            "type": "seminal",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""

    def test_provider_api_active_link_kept(self):
        """Social sources with url_origin=provider_api show their URLs."""
        s = {
            "source_id": "SRC-001",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "active",
            "url_origin": "provider_api",
            "type": "current",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == "10.1000/xyz"
        assert result["active_link"] == "https://sciencedirect.com/page"
        assert result["_url_issues"] == []

    def test_provider_api_dead_link_hidden(self):
        """Provider API URLs still checked for dead/malformed."""
        s = {
            "source_id": "SRC-002",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/dead",
            "link_status": "dead",
            "url_origin": "provider_api",
        }
        result = _validate_source_for_display(s)
        assert result["doi"] == ""
        assert result["active_link"] == ""

    def test_does_not_mutate_original(self):
        s = {
            "source_id": "HIST-011",
            "doi": "10.1000/xyz",
            "active_link": "https://sciencedirect.com/page",
            "link_status": "active",
            "url_origin": "llm_synthesis",
        }
        result = _validate_source_for_display(s)
        assert s["active_link"] == "https://sciencedirect.com/page"
        assert result["active_link"] == ""


# ---------------------------------------------------------------------------
# _format_full_reference — URL issues appear in markdown
# ---------------------------------------------------------------------------

class TestFormatFullReferenceWithIssues:
    def test_no_issues_no_warning(self):
        s = {
            "authors": "Smith",
            "year": 2020,
            "title": "Test Paper",
            "doi": "10.1000/xyz",
            "link_status": "active",
            "_url_issues": [],
        }
        ref = _format_full_reference(s)
        assert "⚠" not in ref

    def test_issues_show_warning(self):
        s = {
            "authors": "Smith",
            "year": 2020,
            "title": "Test Paper",
            "doi": "",
            "active_link": "",
            "link_status": "active",
            "_url_issues": ["URL: malformed URL removed (n/a)"],
        }
        ref = _format_full_reference(s)
        assert "⚠" in ref
        assert "malformed URL removed" in ref
