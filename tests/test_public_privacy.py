import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import mirror


def test_login_prints_do_not_interpolate_credentials_or_page_urls():
    source = Path(__file__).resolve().parents[1] / "reauth.py"
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "print":
            continue
        for value in ast.walk(node):
            if isinstance(value, ast.Name):
                assert value.id not in {"otp", "TRACKER_EMAIL", "GMAIL_APP_PASSWORD"}
            if isinstance(value, ast.Attribute):
                assert not (
                    isinstance(value.value, ast.Name)
                    and value.value.id == "page"
                    and value.attr == "url"
                )


def test_holdings_failure_does_not_include_private_response_body(monkeypatch):
    monkeypatch.setattr(mirror, "load_session_cookie", lambda: "test-cookie")
    monkeypatch.setattr(
        mirror._requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            ok=False, status_code=403, text="private-account-response"
        ),
    )
    with pytest.raises(RuntimeError) as caught:
        mirror.fetch_holdings("2026-01-01")
    assert str(caught.value) == "Holdings fetch failed: 403"
