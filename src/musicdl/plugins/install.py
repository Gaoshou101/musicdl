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

from collections.abc import Mapping, Sequence
from typing import Any

from musicdl.sources.lx.analyzer import LxAnalysis, analyze_source

from .store import PluginStore, StoredPlugin

LANGUAGES = ("javascript", "python")
DEFAULT_OPERATIONS = ("search",)
GRANT_FLAGS = ("allow_insecure_http", "allow_ip_hosts", "allow_any_host")


def _hosts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("allowed_hosts must be a list")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("allowed_hosts must be a list of names")
    return tuple(value)


def _operations(value: Any) -> tuple[Any, ...]:
    if value is None:
        return DEFAULT_OPERATIONS
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
           granted: Mapping[str, Any]) -> StoredPlugin:
    operations = _operations(request.get("operations"))
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
                  allowed_hosts=analysis.allowed_hosts, version=str(version), granted=granted)
