import hashlib

import pytest

from musicdl.plugins.install import install_source
from musicdl.plugins.store import PluginStore


PYTHON_SOURCE = "def handle(request):\n    return []\n"
LX_SOURCE = """/*
 * @name Demo Source
 * @version v3.2.11
 * @author someone
 */
const { EVENT_NAMES, request, on } = globalThis.lx;
const API = 'https://music.example.com/api';
on(EVENT_NAMES.request, ({ info }) => request(`${API}/search?q=${info.keyword}`, { method: 'GET' }));
"""


def test_a_python_source_keeps_the_language_hosts_and_version_it_was_given(tmp_path):
    stored = install_source(PluginStore(tmp_path), {"id": "demo", "language": "python",
                                                    "script": PYTHON_SOURCE, "version": "2",
                                                    "allowed_hosts": ["music.example.com"]})

    assert stored.path.suffix == ".py"
    assert stored.manifest.version == "2"
    assert stored.manifest.operations == ("search",)
    assert stored.manifest.allowed_hosts == ("music.example.com",)
    assert stored.manifest.sha256 == hashlib.sha256(PYTHON_SOURCE.encode()).hexdigest()


def test_a_script_that_is_not_an_lx_source_must_declare_its_language(tmp_path):
    with pytest.raises(ValueError, match="language"):
        install_source(PluginStore(tmp_path), {"id": "demo", "script": PYTHON_SOURCE})


def test_an_lx_source_declares_its_own_language_version_and_allowlist(tmp_path):
    stored = install_source(PluginStore(tmp_path), {"id": "xinghai", "script": LX_SOURCE})

    manifest = stored.manifest
    assert manifest.language == "javascript" and manifest.version == "v3.2.11"
    assert manifest.allowed_hosts == ("music.example.com",)
    assert stored.path.suffix == ".js"


def test_an_lx_source_cannot_widen_the_allowlist_past_its_own_text(tmp_path):
    with pytest.raises(ValueError, match="declares"):
        install_source(PluginStore(tmp_path), {"id": "xinghai", "script": LX_SOURCE,
                                               "allowed_hosts": ["music.example.com", "elsewhere.example.com"]})


def test_a_blocked_lx_source_is_refused_with_the_reason_and_stores_nothing(tmp_path):
    store = PluginStore(tmp_path)
    blocked = LX_SOURCE.replace("https://music.example.com", "http://music.example.com")

    with pytest.raises(ValueError) as failure:
        install_source(store, {"id": "xinghai", "script": blocked})

    assert "plain_http" in str(failure.value)
    assert store.enabled() == ()


def test_a_missing_script_or_id_is_refused(tmp_path):
    store = PluginStore(tmp_path)
    with pytest.raises(ValueError, match="script"):
        install_source(store, {"id": "demo", "language": "python", "script": "  "})
    with pytest.raises(ValueError, match="invalid id"):
        install_source(store, {"id": 7, "language": "python", "script": PYTHON_SOURCE})


def test_operations_must_be_declared_by_the_contract(tmp_path):
    with pytest.raises(ValueError):
        install_source(PluginStore(tmp_path), {"id": "demo", "language": "python",
                                               "script": PYTHON_SOURCE, "operations": ["invent"]})
