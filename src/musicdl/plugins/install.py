"""Administrator-driven installation of one plugin source.

The administration portal owns which sources exist, so the portal request -- not
the data volume -- decides what gets installed here.  The module refuses to
guess: an lx custom source is analysed first, and that analysis decides both the
language and the egress allowlist, so an operator can neither install a script
whose endpoints the broker would refuse nor widen the allowlist past the hosts
the script itself names.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from musicdl.sources.lx.analyzer import LxAnalysis, analyze_source

from .store import PluginStore, StoredPlugin

LANGUAGES = ("javascript", "python")
DEFAULT_OPERATIONS = ("search",)


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


def _store(store: PluginStore, *, plugin_id: str, script: str, request: Mapping[str, Any],
           language: str, allowed_hosts: tuple[str, ...], version: str) -> StoredPlugin:
    operations = _operations(request.get("operations"))
    return store.install(plugin_id=plugin_id, version=version, language=language,
                         operations=operations, allowed_hosts=allowed_hosts, source=script)


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
    if analysis.uses_lx_global or analysis.events:
        return install_lx_source(store, plugin_id=plugin_id, script=script,
                                 request=request, analysis=analysis)
    language = request.get("language")
    if language not in LANGUAGES:
        raise ValueError("language must be javascript or python")
    version = request.get("version")
    if version is None:
        version = "1"
    return _store(store, plugin_id=plugin_id, script=script, request=request, language=str(language),
                  allowed_hosts=_hosts(request.get("allowed_hosts")), version=str(version))


def install_lx_source(store: PluginStore, *, plugin_id: str, script: str,
                      request: Mapping[str, Any], analysis: LxAnalysis) -> StoredPlugin:
    """Install one analysed lx custom source, keeping its own declared endpoints."""
    if analysis.blocked:
        reasons = "; ".join(f"{finding.code}: {finding.detail}" for finding in analysis.blockers)
        raise ValueError(f"blocked lx source: {reasons}")
    if request.get("language") not in (None, "javascript"):
        raise ValueError("an lx custom source is JavaScript")
    declared = request.get("allowed_hosts")
    if declared is not None and set(_hosts(declared)) != set(analysis.allowed_hosts):
        raise ValueError("allowed_hosts for an lx source must be the hosts the script declares")
    version = request.get("version") or analysis.version or "1"
    return _store(store, plugin_id=plugin_id, script=script, request=request, language="javascript",
                  allowed_hosts=analysis.allowed_hosts, version=str(version))
