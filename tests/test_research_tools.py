"""Tests for policy-gated research tools."""
import gzip
from unittest.mock import patch

import httpx
import pytest

from backend.tool_adapters import execute_tool, fetch_url, research_compare, source_summarize, web_search


def test_fetch_url_rejects_non_http_scheme():
    result = fetch_url("file:///etc/passwd")

    assert result["success"] is False
    assert "Scheme not allowed" in result["error"]


def test_fetch_url_rejects_credentials():
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://user:pass@example.com/private")

    assert result["success"] is False
    assert "Credentials" in result["error"]


def test_fetch_url_rejects_localhost_even_if_whitelisted():
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["localhost"]
        result = fetch_url("http://localhost:8000/admin")

    assert result["success"] is False
    assert "Host not allowed" in result["error"]


@pytest.mark.parametrize("ip", ["192.168.1.1", "10.0.0.1", "172.16.0.1", "169.254.1.1"])
def test_fetch_url_rejects_private_ipv4_ranges(ip):
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = [ip]
        result = fetch_url(f"http://{ip}/")

    assert result["success"] is False
    assert "Host not allowed" in result["error"]


@pytest.mark.parametrize("host", ["[::1]", "[fe80::1]", "[fc00::dead:beef]"])
def test_fetch_url_rejects_private_ipv6_ranges(host):
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = [host.strip("[]")]
        result = fetch_url(f"http://{host}/")

    assert result["success"] is False
    assert "Host not allowed" in result["error"]


def test_fetch_url_rejects_unwhitelisted_domain():
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://evil-example.com/")

    assert result["success"] is False
    assert "not whitelisted" in result["error"]


def test_fetch_url_allows_whitelisted_subdomain(httpx_mock):
    httpx_mock.add_response(
        url="https://api.example.com/product",
        text="Agent workflow research data.",
        headers={"content-type": "text/plain"},
    )
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://api.example.com/product")

    assert result["success"] is True
    assert result["data"]["url"] == "https://api.example.com/product"


def test_fetch_url_fetches_redacts_and_tracks_provenance(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/product",
        text="<html><body><h1>Product</h1><p>Agent workflow and research automation.</p><p>token=placeholder-secret-value</p></body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/product", max_bytes=4096)

    assert result["success"] is True
    data = result["data"]
    assert data["url"] == "https://example.com/product"
    assert data["source_id"]
    assert data["provenance"]["tool"] == "fetch_url"
    assert "placeholder-secret-value" not in data["content"]
    assert "[REDACTED]" in data["content"]


def test_fetch_url_rejects_missing_content_type(httpx_mock):
    httpx_mock.add_response(url="https://example.com/file", content=b"binary-ish data")
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/file")

    assert result["success"] is False
    assert "Content type not allowed or missing" in result["error"]


def test_fetch_url_rejects_compressed_responses_before_body_processing(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/bomb",
        content=gzip.compress(b"compressed-placeholder"),
        headers={"content-type": "text/html", "content-encoding": "gzip"},
    )
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/bomb")

    assert result["success"] is False
    assert "Compressed responses not allowed" in result["error"]


def test_fetch_url_rejects_redirect_to_unwhitelisted_domain(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/start",
        status_code=302,
        headers={"location": "https://evil.com/internal"},
    )
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/start")

    assert result["success"] is False
    assert "not whitelisted" in result["error"]


def test_fetch_url_rejects_excessive_redirects(httpx_mock):
    for index in range(4):
        httpx_mock.add_response(
            url=f"https://example.com/r{index}",
            status_code=302,
            headers={"location": f"https://example.com/r{index + 1}"},
        )
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/r0")

    assert result["success"] is False
    assert "Too many redirects" in result["error"]


def test_fetch_url_truncates_large_responses(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/large",
        text="x" * 300000,
        headers={"content-type": "text/plain"},
    )
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/large", max_bytes=4096)

    assert result["success"] is True
    assert result["data"]["truncated"] is True
    assert result["data"]["bytes_read"] == 4096


def test_fetch_url_reports_timeout(httpx_mock):
    httpx_mock.add_exception(httpx.TimeoutException("slow source"), url="https://example.com/slow")
    with patch("backend.tool_adapters.settings") as mock_settings:
        mock_settings.whitelisted_domains = ["example.com"]
        result = fetch_url("https://example.com/slow", timeout_seconds=1)

    assert result["success"] is False
    assert "Timed out" in result["error"]


def test_web_search_is_policy_controlled_placeholder():
    result = web_search("agent platforms")

    assert result["success"] is False
    assert "not configured" in result["error"]


def test_source_summarize_returns_source_backed_redacted_summary():
    result = source_summarize(
        source={
            "url": "https://example.com/product",
            "title": "Example Product",
            "source_id": "src1",
            "content": "Example Product has agent workflow automation. Pricing starts with a free plan. API_KEY=abcdefghijklmnop",
        }
    )

    assert result["success"] is True
    data = result["data"]
    assert data["artifact_type"] == "source_summary"
    assert data["source"]["source_id"] == "src1"
    assert data["pricing_notes"]
    assert "abcdefghijklmnop" not in str(data)


def test_research_compare_builds_research_brief_with_sources():
    result = research_compare(
        topic="AI coworker competitors",
        sources=[
            {
                "source": {"title": "Example Agent", "url": "https://example.com/agent", "source_id": "src-agent"},
                "summary_points": ["Example Agent positions itself as an automation product for teams."],
                "feature_notes": ["It includes agent workflow automation and research workflow support."],
                "pricing_notes": ["Pricing includes a free tier and paid plans."],
            }
        ],
    )

    assert result["success"] is True
    data = result["data"]
    assert data["artifact_type"] == "research_brief"
    assert data["competitor_list"][0]["source_id"] == "src-agent"
    assert data["feature_matrix"]["rows"][0]["features"]["Agent workflow"] is True
    assert data["provenance"][0]["url"] == "https://example.com/agent"


def test_execute_tool_dispatches_research_tools():
    result = execute_tool(
        "source_summarize",
        {"text": "This product offers browser automation and research workflow features.", "url": "https://example.com"},
    )

    assert result["success"] is True
    assert result["data"]["artifact_type"] == "source_summary"
