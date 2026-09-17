import hashlib

import pytest

from musicdl.contracts import MAX_SOURCE_BYTES
from musicdl.plugins.install import (install_lx_source, install_source, preview_source,
                                     suggest_plugin_id)
from musicdl.plugins.store import PluginStore, validate_plugin_id
from musicdl.sources.lx.analyzer import analyze_source


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
    # The language, the version, and the allowlist are the script's to declare;
    # the one grant every lx source needs is still the operator's to give.
    stored = install_source(PluginStore(tmp_path), {"id": "xinghai", "script": LX_SOURCE,
                                                    "allow_any_host": True,
                                                    "allow_insecure_http": True})

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
    # A real HTTP verb the broker cannot perform is not a policy question, so no
    # grant can rescue it.
    blocked = LX_SOURCE.replace("method: 'GET'", "method: 'PUT'")

    with pytest.raises(ValueError) as failure:
        install_source(store, {"id": "xinghai", "script": blocked})

    assert "unsupported_method" in str(failure.value)
    assert store.enabled() == ()


WIDENED_SOURCE = """/*
 * @name Widened Source
 */
const { EVENT_NAMES, request, on } = globalThis.lx;
const API = 'http://103.79.184.97:8928/api';
on(EVENT_NAMES.request, ({ info }) => request(`${API}/search?q=${info.keyword}`, { method: 'POST' }));
"""


def test_a_widened_lx_source_installs_only_once_the_operator_grants_it(tmp_path):
    store = PluginStore(tmp_path)
    definition = {"id": "widened", "script": WIDENED_SOURCE}

    with pytest.raises(ValueError) as failure:
        install_source(store, dict(definition))

    # Nothing is widened silently: the refusal names the grants to add.
    assert "allow_any_host" in str(failure.value)
    assert "allow_insecure_http" in str(failure.value) and "allow_ip_hosts" in str(failure.value)
    assert "allowed_ports" in str(failure.value) and store.enabled() == ()

    stored = install_source(store, dict(definition, allow_insecure_http=True, allow_ip_hosts=True,
                                        allowed_ports=[443, 8928], allow_any_host=True))

    egress = stored.manifest.egress
    assert egress.allowed_hosts == ("103.79.184.97",)
    assert egress.allowed_ports == (443, 8928)
    assert egress.allow_insecure_http is True and egress.allow_ip_hosts is True
    # Open egress is the one grant every lx source needs, because the media URL
    # it resolves to is assembled while it runs.
    assert egress.allow_any_host is True


def test_a_grant_nobody_asked_for_is_still_the_operators_choice(tmp_path):
    stored = install_source(PluginStore(tmp_path), {"id": "xinghai", "script": LX_SOURCE,
                                                    "allow_any_host": True,
                                                    "allow_insecure_http": True})

    assert stored.manifest.egress.allow_any_host is True
    assert stored.manifest.allowed_hosts == ("music.example.com",)


def test_an_opaque_source_needs_open_egress_and_says_so(tmp_path):
    store = PluginStore(tmp_path)
    opaque = LX_SOURCE.replace("'https://music.example.com/api'", "'\\x6d\\x75\\x73\\x69\\x63\\x2e\\x63\\x6f\\x6d'")

    with pytest.raises(ValueError, match="allow_any_host"):
        install_source(store, {"id": "opaque", "script": opaque})

    stored = install_source(store, {"id": "opaque", "script": opaque, "allow_any_host": True,
                                    "allow_insecure_http": True})

    assert stored.manifest.egress.allow_any_host is True


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


def test_an_lx_source_cannot_be_installed_without_open_egress(tmp_path):
    """The one grant every lx source needs, asked for by name.

    A resolved media URL is assembled while the script runs, so the host it
    points at is not something the analysis can allowlist.  The install has to be
    refused rather than storing a source whose downloads then fail on a host the
    operator never saw.
    """
    analysis = analyze_source(LX_SOURCE, path="demo.js")
    store = PluginStore(tmp_path)

    with pytest.raises(ValueError, match="allow_any_host"):
        install_lx_source(store, plugin_id="demo", script=LX_SOURCE, request={}, analysis=analysis)

    assert store.enabled() == ()
    stored = install_lx_source(store, plugin_id="demo", script=LX_SOURCE,
                               request={"allow_any_host": True, "allow_insecure_http": True},
                               analysis=analysis)
    assert stored.manifest.egress.allow_any_host is True
    assert stored.manifest.egress.allow_insecure_http is True
    assert stored.manifest.operations == ("search", "resolve")


LX_GRANTS = {"allow_any_host": True, "allow_insecure_http": True}


def test_a_preview_names_the_grants_an_lx_source_still_needs():
    preview = preview_source({"id": "demo", "filename": "星海音乐源 v2.3.11.js", "script": LX_SOURCE})

    assert (preview["install_path"], preview["language"]) == ("lx", "javascript")
    assert preview["operations"] == ["search", "resolve"]
    assert preview["required_grants"] == {"allow_any_host": True, "allow_insecure_http": True}
    assert preview["missing_grants"] == ["allow_any_host", "allow_insecure_http"]
    assert preview["installable"] is False and "allow_any_host" in preview["refusal"]
    assert preview["id"] == {"value": "demo", "valid": True, "reason": None, "suggested": "demo-source"}
    assert preview["analysis"]["name"] == "Demo Source"
    assert preview["analysis"]["sha256"] == hashlib.sha256(LX_SOURCE.encode()).hexdigest()


def test_a_preview_of_a_granted_lx_source_says_the_import_would_succeed():
    preview = preview_source({"id": "demo", "script": LX_SOURCE, **LX_GRANTS})

    assert preview["installable"] is True and preview["refusal"] is None
    assert preview["missing_grants"] == []
    assert preview["granted"] == {"allow_any_host": True, "allow_insecure_http": True}


def test_a_preview_of_a_blocked_source_carries_the_analyzer_reason():
    preview = preview_source({"id": "demo", "script": LX_SOURCE.replace("'GET'", "'PUT'"), **LX_GRANTS})

    assert preview["installable"] is False and "unsupported_method" in preview["refusal"]
    assert [item["code"] for item in preview["analysis"]["blockers"]] == ["unsupported_method"]


def test_a_preview_of_a_generic_script_asks_for_its_language():
    preview = preview_source({"id": "demo", "script": PYTHON_SOURCE})

    assert preview["install_path"] == "generic" and preview["required_grants"] == {}
    assert preview["missing_grants"] == [] and preview["installable"] is False
    assert "language" in preview["refusal"]
    assert preview_source({"id": "demo", "language": "python", "script": PYTHON_SOURCE})["installable"] is True


def test_a_preview_and_the_install_it_previews_agree_about_every_refusal(tmp_path):
    """The preview is a claim about an install, so an install has to settle it.

    Each case below is one way an import can be refused, and the preview's
    answer and the install's answer have to match on all of them -- which is the
    only thing that keeps a screen offering the import from disagreeing with the
    import itself.
    """
    python = {"id": "demo", "language": "python", "script": PYTHON_SOURCE}
    bodies = [
        python,
        {**python, "language": "ruby"},
        {**python, "id": "not an id"},
        {"id": "demo", "script": LX_SOURCE},
        {"id": "demo", "script": LX_SOURCE, **LX_GRANTS},
        {"id": "demo", "script": LX_SOURCE.replace("'GET'", "'PUT'"), **LX_GRANTS},
        {"id": "demo", "script": LX_SOURCE, "language": "python", **LX_GRANTS},
        {"id": "demo", "script": LX_SOURCE, "allowed_hosts": ["elsewhere.example.com"], **LX_GRANTS},
        {"id": "demo", "script": LX_SOURCE, "version": "v" * 100, **LX_GRANTS},
        {"id": "demo", "script": LX_SOURCE + "\x00", **LX_GRANTS},
        {"id": "demo", "script": LX_SOURCE + "x" * MAX_SOURCE_BYTES, **LX_GRANTS},
    ]

    for index, body in enumerate(bodies):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        store = PluginStore(case)
        try:
            install_source(store, body)
        except ValueError as exc:
            refused: str | None = str(exc)
        else:
            refused = None
        preview = preview_source(body)
        assert preview["installable"] is (refused is None), body
        assert (preview["refusal"] is None) is (refused is None), body


def test_a_suggested_id_is_an_id_the_store_would_take():
    for name in ["星海音乐源 v2.3.11.js", "K×H测试 v1.7.17.js", "聚合音源 特供版.js", "lx-music-source-v6 (修复).js"]:
        validate_plugin_id(suggest_plugin_id(name))


def test_a_suggested_id_keeps_the_source_and_drops_its_version():
    assert suggest_plugin_id("lx-music-source-v6 (修复).js") == "lx-music-source"
    assert suggest_plugin_id("lx-玉宁熙-Pro v1.2.2 (1).js") == "lx-pro"
    assert suggest_plugin_id("HYWmusic_beta_公益测试 v0.74.0.js") == "hywmusic-beta"
    assert suggest_plugin_id("Demo Source", digest="a" * 64) == "demo-source"


def test_a_name_with_no_ascii_gets_one_placeholder_for_every_version_of_it():
    """Six of the twelve real sources are named only in Chinese.

    Their file names cannot become ids, so the suggestion is a placeholder --
    but it is the *same* placeholder for every version, because importing v2 is
    meant to replace v1 rather than to start a second source.
    """
    first = suggest_plugin_id("星海音乐源", "星海音乐源 v2.3.11.js")
    second = suggest_plugin_id("星海音乐源", "星海音乐源 v2.4.0.js")

    assert first == second and first.startswith("lx-source-")
    validate_plugin_id(first)
