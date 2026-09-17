"""Administrator-driven installation of one plugin source.

The administration portal owns which sources exist, so the portal request -- not
the data volume -- decides what gets installed here.  The module refuses to
guess: an lx custom source is analysed first, and that analysis decides the
language, the egress allowlist, and the *grants* the source still needs.

A grant is never implied.  Plain HTTP, an address literal, a non-standard port,
and "any host" each widen the egress policy, so each one has to be requested
explicitly by the operator and is then recorded on the manifest, where the
broker enforces exactly that and nothing more.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from musicdl.sources.lx.analyzer import LxAnalysis, analyze_source

from .store import PLUGIN_ID_PATTERN, PluginStore, StoredPlugin, validate_plugin_id

LANGUAGES = ("javascript", "python")
DEFAULT_OPERATIONS = ("search",)
# An lx custom source answers `musicUrl`, which is this project's `resolve`.  A
# search action is optional and only some sources declare one, so both are
# published: a source that cannot search then fails honestly at search time
# instead of being installed as a source nothing can ever be downloaded from.
LX_DEFAULT_OPERATIONS = ("search", "resolve")
GRANT_FLAGS = ("allow_insecure_http", "allow_ip_hosts", "allow_any_host")


def _hosts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("allowed_hosts must be a list")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("allowed_hosts must be a list of names")
    return tuple(value)


def _operations(value: Any, default: tuple[Any, ...] = DEFAULT_OPERATIONS) -> tuple[Any, ...]:
    if value is None:
        return default
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise ValueError("operations must be a non-empty list")
    return tuple(value)


def _grants(request: Mapping[str, Any]) -> dict[str, Any]:
    """The egress widenings one portal request asks for, validated as written."""
    granted: dict[str, Any] = {}
    for key in GRANT_FLAGS:
        value = request.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        granted[key] = value
    ports = request.get("allowed_ports")
    if ports is not None:
        if isinstance(ports, str) or not isinstance(ports, Sequence) or not ports:
            raise ValueError("allowed_ports must be a non-empty list")
        if any(isinstance(item, bool) or not isinstance(item, int) or not 0 < item <= 65535 for item in ports):
            raise ValueError("allowed_ports must be a list of ports")
        granted["allowed_ports"] = tuple(sorted(set(ports)))
    return granted


def _missing_grants(required: Mapping[str, Any], granted: Mapping[str, Any]) -> list[str]:
    """Which of the widenings a source needs the operator has not yet allowed."""
    missing: list[str] = []
    for key, value in required.items():
        if key == "allowed_ports":
            if not set(value) <= set(granted.get(key) or ()):
                missing.append(key)
        elif granted.get(key) is not True:
            missing.append(key)
    return sorted(missing)


def _store(store: PluginStore, *, plugin_id: str, script: str, request: Mapping[str, Any],
           language: str, allowed_hosts: tuple[str, ...], version: str,
           granted: Mapping[str, Any],
           default_operations: tuple[Any, ...] = DEFAULT_OPERATIONS) -> StoredPlugin:
    operations = _operations(request.get("operations"), default_operations)
    return store.install(plugin_id=plugin_id, version=version, language=language,
                         operations=operations, allowed_hosts=allowed_hosts,
                         allowed_ports=granted.get("allowed_ports") or (443,),
                         allow_insecure_http=bool(granted.get("allow_insecure_http")),
                         allow_ip_hosts=bool(granted.get("allow_ip_hosts")),
                         allow_any_host=bool(granted.get("allow_any_host")), source=script)


def install_source(store: PluginStore, request: Mapping[str, Any]) -> StoredPlugin:
    """Store the script one portal request carries, or explain why not."""
    if not isinstance(request, Mapping):
        raise ValueError("invalid request")
    plugin_id, script = request.get("id"), request.get("script")
    if not isinstance(plugin_id, str):
        raise ValueError("invalid id")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("script is required")
    analysis = analyze_source(script)
    if analysis.lx_shaped:
        return install_lx_source(store, plugin_id=plugin_id, script=script,
                                 request=request, analysis=analysis)
    language = request.get("language")
    if language not in LANGUAGES:
        raise ValueError("language must be javascript or python")
    version = request.get("version")
    if version is None:
        version = "1"
    return _store(store, plugin_id=plugin_id, script=script, request=request, language=str(language),
                  allowed_hosts=_hosts(request.get("allowed_hosts")), version=str(version),
                  granted=_grants(request))


def install_lx_source(store: PluginStore, *, plugin_id: str, script: str,
                      request: Mapping[str, Any], analysis: LxAnalysis) -> StoredPlugin:
    """Install one analysed lx custom source under the grants it needs."""
    if analysis.blocked:
        reasons = "; ".join(f"{finding.code}: {finding.detail}" for finding in analysis.blockers)
        raise ValueError(f"blocked lx source: {reasons}")
    if request.get("language") not in (None, "javascript"):
        raise ValueError("an lx custom source is JavaScript")
    declared = request.get("allowed_hosts")
    if declared is not None and set(_hosts(declared)) != set(analysis.allowed_hosts):
        raise ValueError("allowed_hosts for an lx source must be the hosts the script declares")
    granted = _grants(request)
    missing = _missing_grants(analysis.required_grants, granted)
    if missing:
        raise ValueError("lx source needs an explicit operator grant: " + ", ".join(missing))
    version = request.get("version") or analysis.version or "1"
    return _store(store, plugin_id=plugin_id, script=script, request=request, language="javascript",
                  allowed_hosts=analysis.allowed_hosts, version=str(version), granted=granted,
                  default_operations=LX_DEFAULT_OPERATIONS)


# A source file arrives here called something like
# ``星海音乐源 v2.3.11.js`` or ``K×H测试 v1.7.17.js``.  The version is the part
# that must not reach the id: importing v2 has to name the same source v1 named.
_VERSION_TAIL = re.compile(r"[\s_-]*v?[\s._-]*\d+(?:[._-]\d+)*\s*$", re.IGNORECASE)
# The same rule once the name has become a slug, for a version the name spelled
# apart from the words it belongs to: ``lx-pro-v1-2-2-1`` is ``lx-pro``.
_SLUG_VERSION_TAIL = re.compile(r"(?:-v?\d+)+$")
_SOURCE_SUFFIXES = (".js", ".mjs", ".cjs", ".py")


def _stem(value: str) -> str:
    """A file name or a declared name, without its directory, extension or version."""
    stem = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in _SOURCE_SUFFIXES:
        if stem.lower().endswith(suffix):
            return _VERSION_TAIL.sub("", stem[: -len(suffix)]).strip()
    return _VERSION_TAIL.sub("", stem).strip()


def suggest_plugin_id(*candidates: Any, digest: str = "") -> str:
    """A valid plugin id for an operator who has not chosen one yet.

    The source files this portal is fed are named in Chinese, and a file name is
    not an id: the store keeps ids to an ASCII slug.  So the suggestion is a
    prefill the operator can overrule, never a derived identity -- two versions
    of one source are one source, and only the operator knows which id that is.
    A name with no ASCII left in it still gets a suggestion, taken from the name
    rather than from the script, so the same source is offered the same
    placeholder whichever version of it is being imported.
    """
    stems = [stem for stem in (_stem(item) for item in candidates if isinstance(item, str)) if stem]
    for stem in stems:
        words = [part for part in re.split(r"[^A-Za-z0-9]+", stem.lower()) if part]
        slug = _SLUG_VERSION_TAIL.sub("", "-".join(words))[:64].strip("-")
        # A slug with no letters left in it named nothing: ``v3`` is a version
        # that lost its source.  Whatever does survive still has to be an id.
        if re.search(r"[a-z]", slug) and PLUGIN_ID_PATTERN.fullmatch(slug):
            return slug
    subject = stems[0] if stems else digest
    return f"lx-source-{hashlib.sha256(subject.encode()).hexdigest()[:8]}" if subject else "lx-source"


def _identity(request: Mapping[str, Any], analysis: LxAnalysis) -> dict[str, Any]:
    """The id this install would use, and whether the store would take it."""
    value = request.get("id")
    suggested = suggest_plugin_id(analysis.name, request.get("filename"), digest=analysis.sha256)
    try:
        validate_plugin_id(value)
    except ValueError as exc:
        return {"value": value if isinstance(value, str) else None, "valid": False,
                "reason": str(exc), "suggested": suggested}
    return {"value": value, "valid": True, "reason": None, "suggested": suggested}


def preview_source(request: Mapping[str, Any]) -> dict[str, Any]:
    """What installing this script would do, decided without storing it.

    The portal offers an import before it performs one, and the screen that
    offers it has to answer three questions on its own: what the script says
    about itself, which egress widenings it needs, and which of those this
    request already grants.  The third answer is not re-derived from the rules:
    the same install is run against a throwaway store, so a preview that calls
    an import installable cannot disagree with the import the operator then
    performs.  Nothing of the script outlives the preview -- the scratch
    directory goes with it.
    """
    if not isinstance(request, Mapping):
        raise ValueError("invalid request")
    script = request.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("script is required")
    filename = request.get("filename")
    analysis = analyze_source(script, path=filename if isinstance(filename, str) else None)
    lx_shaped = analysis.lx_shaped
    granted = _grants(request)
    required = analysis.required_grants if lx_shaped else {}
    operations = _operations(request.get("operations"),
                             LX_DEFAULT_OPERATIONS if lx_shaped else DEFAULT_OPERATIONS)
    identity = _identity(request, analysis)
    with tempfile.TemporaryDirectory(prefix="musicdl-preview-") as scratch:
        try:
            install_source(PluginStore(scratch), request)
        except ValueError as exc:
            refusal: str | None = str(exc)
        else:
            refusal = None
    return {"id": identity, "install_path": "lx" if lx_shaped else "generic",
            "language": "javascript" if lx_shaped else request.get("language"),
            "operations": list(operations),
            "required_grants": required,
            "granted": {key: list(value) if isinstance(value, tuple) else value
                        for key, value in granted.items()},
            "missing_grants": _missing_grants(required, granted) if lx_shaped else [],
            "installable": refusal is None, "refusal": refusal,
            "analysis": analysis.summary()}
