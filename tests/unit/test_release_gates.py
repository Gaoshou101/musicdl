"""The callback gate has to honour both expectations it advertises.

`--wecom-expected` documents a status *or* a body. It used to evaluate
`int(expected)` first, so a body expectation raised inside the probe and the
gate reported FAIL for a callback that had answered correctly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_gates():
    spec = importlib.util.spec_from_file_location("musicdl_run_gates", ROOT / "scripts/release/run_gates.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gates = _load_gates()
CALLBACK = "https://example.invalid/wecom/callback?msg_signature=0"


class _Reply:
    def __init__(self, status: int, body: str) -> None:
        self.status, self._body = status, body.encode("utf-8")

    def read(self, _size: int | None = None) -> bytes:
        return self._body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _answer(monkeypatch, status: int, body: str) -> None:
    monkeypatch.setattr(gates.urllib.request, "urlopen", lambda url, timeout=None: _Reply(status, body))


def test_a_status_expectation_matches_the_status(monkeypatch) -> None:
    _answer(monkeypatch, 200, "")
    assert gates.wecom(CALLBACK, "200") is True


def test_a_body_expectation_matches_the_body(monkeypatch) -> None:
    _answer(monkeypatch, 200, "musicdl-release-echostr-00ff")
    assert gates.wecom(CALLBACK, "musicdl-release-echostr-00ff") is True


def test_a_body_expectation_that_the_callback_did_not_echo_fails(monkeypatch) -> None:
    _answer(monkeypatch, 200, "musicdl-release-echostr-00ff")
    assert gates.wecom(CALLBACK, "musicdl-release-echostr-11aa") is False


def test_a_numeric_expectation_still_has_to_match_the_status(monkeypatch) -> None:
    _answer(monkeypatch, 400, "bad request")
    assert gates.wecom(CALLBACK, "200") is False


def test_the_probe_refuses_a_plain_http_target() -> None:
    assert gates.wecom("http://example.invalid/wecom/callback", "200") is False


def test_the_gate_is_not_run_without_the_opt_in() -> None:
    assert gates.wecom() is False
