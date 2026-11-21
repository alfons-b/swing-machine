import pytest

from idx_trading.ingestion.http import HttpClient, redact


def test_redact_hides_tokens():
    assert redact("https://x/api?a=1&api_token=SECRET&b=2") == "https://x/api?a=1&api_token=***&b=2"
    assert "SECRET" not in redact("https://x/?apikey=SECRET")


def test_retries_transient_errors_with_backoff_and_retry_after():
    sleeps, calls = [], []

    def transport(url, headers, timeout):
        calls.append(url)
        if len(calls) < 3:
            return 503, {"Retry-After": "7"}, b""
        return 200, {"Content-Type": "application/json"}, b"[]"

    c = HttpClient(transport=transport, sleep=sleeps.append, backoff_seconds=1)
    r = c.get("https://example.test/api", {"api_token": "s"})
    assert r.status == 200 and len(calls) == 3
    assert sleeps == [7.0, 7.0]  # Retry-After wins over the 1s/2s backoff
    assert "api_token=***" in r.url


def test_does_not_retry_auth_errors():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return 403, {}, b"no"

    r = HttpClient(transport=transport, sleep=lambda _s: None).get("https://example.test/")
    assert r.status == 403 and len(calls) == 1


def test_gives_up_after_max_retries():
    r = HttpClient(transport=lambda u, h, t: (429, {}, b""), sleep=lambda _s: None, max_retries=2).get(
        "https://example.test/"
    )
    assert r.status == 429


def test_network_errors_retry_then_raise():
    def boom(url, headers, timeout):
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError, match="reset"):
        HttpClient(transport=boom, sleep=lambda _s: None, max_retries=1).get(
            "https://example.test/?token=abc"
        )


def test_min_interval_throttles():
    t = [0.0]
    sleeps = []
    c = HttpClient(
        transport=lambda u, h, to: (200, {}, b""),
        min_interval_seconds=0.5,
        sleep=sleeps.append,
        clock=lambda: t[0],
    )
    c.get("https://example.test/")
    c.get("https://example.test/")
    assert sleeps == [0.5]


def test_https_only():
    with pytest.raises(ValueError):
        HttpClient().get("http://example.test/")


def test_settings_do_not_leak_dotenv_into_tests():
    from idx_trading.config import get_settings

    assert get_settings().eodhd_api_token is None
