"""Static capability analysis for lx-music custom source scripts.

The plugin egress contract served by ``musicdl.plugins.broker`` is exact: HTTPS
GET requests to named DNS hosts on port 443, filtered by a per-source allowlist
taken from the plugin manifest.  A custom source may therefore only be installed
once this analysis shows that the endpoints the script declares fit that
contract, because the broker denies every host the analysis failed to derive.  A
miss is fail-closed, so the point of this module is to make the *reason* for each
refusal traceable to the script text instead of to a guess.

Nothing here executes the script.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from musicdl.contracts import MAX_SOURCE_BYTES

Verdict = Literal["installable", "blocked"]

_META = re.compile(r"^[ \t]*\*?[ \t]*@(?P<key>[A-Za-z][A-Za-z0-9]*)[ \t]+(?P<value>\S.*?)[ \t]*$", re.MULTILINE)
_URL = re.compile(r"(?P<scheme>https?):[/]{2}(?P<rest>[^\s\"`'\\<>(){}\[\]]*)")
_METHOD = re.compile(r"\bmethod\b\s*[:=]\s*[\"'](?P<method>[A-Za-z]+)[\"']")
_LX_GLOBAL = re.compile(r"globalThis\s*(?:\.\s*lx|\[\s*[\"']lx[\"']\s*\])")
_EVENT = re.compile(r"EVENT_NAMES\s*(?:\.\s*(?P<dot>[A-Za-z_][A-Za-z0-9_]*)|\[\s*[\"'](?P<index>[A-Za-z_][A-Za-z0-9_]*)[\"']\s*\])")
_ESCAPED = re.compile(r"(?:\\x[0-9a-fA-F]{2}){6,}|(?:\\u[0-9a-fA-F]{4}){6,}")
_HOSTLIKE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_DYNAMIC_HOST_CHARS = frozenset("${}+~,|^=%?*!")
_METADATA_FIELDS = frozenset({"name", "version", "author", "license", "homepage", "description"})

# The broker implements exactly one method, so anything else is unsupported by
# construction rather than by policy.
_SUPPORTED_METHODS = frozenset({"get"})


@dataclass(frozen=True)
class LxFinding:
    """One refusal, with the evidence that produced it."""

    code: str
    detail: str


@dataclass(frozen=True)
class LxAnalysis:
    """Everything the portal and an installer need in order to decide."""

    sha256: str
    size_bytes: int
    name: str | None = None
    version: str | None = None
    author: str | None = None
    license: str | None = None
    homepage: str | None = None
    description: str | None = None
    path: str | None = None
    uses_lx_global: bool = False
    events: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    referenced_hosts: tuple[str, ...] = ()
    schemes: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    dynamic_endpoints: tuple[str, ...] = ()
    blockers: tuple[LxFinding, ...] = ()
    caveats: tuple[LxFinding, ...] = ()

    @property
    def verdict(self) -> Verdict:
        return "blocked" if self.blockers else "installable"

    @property
    def blocked(self) -> bool:
        return self.verdict == "blocked"

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.blockers)

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """The allowlist an install hands to the manifest.

        Hosts the script mentions as bare domain literals are included on
        purpose: a script that assembles ``https://${domain}/path`` keeps its
        domains in a table, and every host in that table appears here as a
        literal.  A host built from data the script downloaded never appears in
        its text, stays out of the allowlist, and is denied at request time.
        """
        return tuple(sorted(set(self.endpoints) | set(self.referenced_hosts)))

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "author": self.author,
                "license": self.license, "homepage": self.homepage,
                "description": self.description, "sha256": self.sha256,
                "size_bytes": self.size_bytes, "path": self.path, "verdict": self.verdict,
                "events": list(self.events), "endpoints": list(self.endpoints),
                "referenced_hosts": list(self.referenced_hosts),
                "allowed_hosts": list(self.allowed_hosts), "schemes": list(self.schemes),
                "methods": list(self.methods), "dynamic_endpoints": list(self.dynamic_endpoints),
                "blockers": [{"code": item.code, "detail": item.detail} for item in self.blockers],
                "caveats": [{"code": item.code, "detail": item.detail} for item in self.caveats]}


def strip_leading_comments(source: str) -> str:
    """Drop the file header comment and scan the body exactly as written.

    The header carries ``@homepage`` and prose, and prose names hosts the script
    never contacts.  Only a leading run of comments is removed, because a
    general JavaScript comment stripper needs a complete tokenizer: one that
    guesses wrong at a regex or template literal deletes real code, and a
    silently truncated script analyses as something it is not.

    Leaving body comments in place can only widen the derived allowlist with a
    host the script itself names, and the operator reviews that list.
    """
    index, length = 0, len(source)
    while index < length:
        while index < length and source[index].isspace():
            index += 1
        if source.startswith("//", index):
            while index < length and source[index] not in "\r\n":
                index += 1
            continue
        if source.startswith("/*", index):
            closing = source.find("*/", index + 2)
            return "" if closing == -1 else strip_leading_comments(source[closing + 2:])
        break
    return source[index:]


def _is_hostname_literal(value: str) -> bool:
    """A bare domain a script keeps as a string: not an address, not a version."""
    candidate = value.strip().lower()
    if not candidate or len(candidate) > 253 or not _HOSTLIKE.fullmatch(candidate):
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return False
    return candidate.rsplit(".", 1)[-1].isalpha()


def string_literals(source: str) -> tuple[str, ...]:
    """Every quoted literal, with template holes kept as dynamic text."""
    values: list[str] = []
    index, length = 0, len(source)
    while index < length:
        if source[index] not in "\"'`":
            index += 1
            continue
        quote, index, buffer = source[index], index + 1, []
        while index < length:
            current = source[index]
            if current == "\\" and index + 1 < length:
                buffer.append(source[index:index + 2])
                index += 2
                continue
            if current == quote:
                index += 1
                break
            if quote == "`" and current == "$" and index + 1 < length and source[index + 1] == "{":
                closing = _template_hole_end(source, index + 2)
                buffer.append(source[index:closing])
                index = closing
                continue
            buffer.append(current)
            index += 1
        values.append("".join(buffer))
    return tuple(values)


def _template_hole_end(source: str, index: int) -> int:
    depth = 1
    while index < len(source) and depth:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char in "\"'`":
            quote, index = char, index + 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    break
                index += 1
        index += 1
    return index


def _host_from_url(rest: str) -> tuple[str, bool]:
    """Return ``(host, dynamic)`` for the authority part of an absolute URL."""
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0].split("@")[-1]
    if ":" in authority:
        authority = authority.split(":", 1)[0]
    dynamic = not authority or any(char in authority for char in _DYNAMIC_HOST_CHARS)
    return authority.lower(), dynamic


def _classify_host(host: str) -> LxFinding | None:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return LxFinding("ip_literal_host", f"endpoint {host!r} is an IP literal with no DNS name to pin")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return LxFinding("invalid_host", f"endpoint {host!r} is not a resolvable DNS name")
    if ascii_host != host or not _HOSTLIKE.fullmatch(host) or len(host) > 253:
        return LxFinding("invalid_host", f"endpoint {host!r} is not a plain lowercase DNS name")
    return None


def analyze_source(source: str, *, path: str | None = None) -> LxAnalysis:
    """Analyse one custom source without executing it."""
    source_bytes = source.encode("utf-8")
    code = strip_leading_comments(source)

    metadata: dict[str, str] = {}
    for match in _META.finditer(source[:4096]):
        key = match.group("key").lower()
        if key in _METADATA_FIELDS and key not in metadata:
            metadata[key] = match.group("value")

    endpoints: set[str] = set()
    dynamic: set[str] = set()
    schemes: set[str] = set()
    for match in _URL.finditer(code):
        schemes.add(match.group("scheme").lower())
        host, is_dynamic = _host_from_url(match.group("rest"))
        if is_dynamic:
            dynamic.add(match.group(0))
            continue
        endpoints.add(host)

    referenced = {value.strip().lower() for value in string_literals(code)
                  if _is_hostname_literal(value)}
    methods = {match.group("method").lower() for match in _METHOD.finditer(code)}
    events = {(match.group("dot") or match.group("index")) for match in _EVENT.finditer(code)}
    uses_lx_global = bool(_LX_GLOBAL.search(code))

    blockers: list[LxFinding] = []
    caveats: list[LxFinding] = []
    if len(source_bytes) > MAX_SOURCE_BYTES:
        blockers.append(LxFinding("source_too_large",
                                  f"{len(source_bytes)} bytes exceeds the {MAX_SOURCE_BYTES} byte plugin limit"))
    if not uses_lx_global and not events:
        blockers.append(LxFinding("unsupported_shape",
                                  "script neither reads globalThis.lx nor registers an lx event handler"))
    if _ESCAPED.search(code):
        blockers.append(LxFinding("obfuscated_strings",
                                  "string table is stored as escaped code points, so no endpoint can be derived"))
    if "http" in schemes:
        blockers.append(LxFinding("plain_http", "script builds plain http:// URLs, which the broker refuses"))
    for host in sorted(endpoints):
        finding = _classify_host(host)
        if finding is not None:
            blockers.append(finding)
    for method in sorted(methods - _SUPPORTED_METHODS):
        blockers.append(LxFinding("unsupported_method",
                                  f"script selects method {method.upper()!r}; the broker only performs GET"))
    for value in sorted(dynamic):
        # Not a refusal: the broker only ever reaches hosts on the derived
        # allowlist, so an unlisted host fails closed at request time.
        caveats.append(LxFinding("dynamic_endpoint",
                                 f"endpoint {value!r} is assembled at runtime from other values"))
    if not endpoints and referenced:
        caveats.append(LxFinding("literal_hosts_only",
                                 "every endpoint comes from bare domain literals rather than absolute URLs"))
    if not endpoints and not referenced:
        blockers.append(LxFinding("no_endpoints", "script declares no endpoint this analysis can derive"))

    return LxAnalysis(
        sha256=hashlib.sha256(source_bytes).hexdigest(), size_bytes=len(source_bytes), path=path,
        name=metadata.get("name"), version=metadata.get("version"), author=metadata.get("author"),
        license=metadata.get("license"), homepage=metadata.get("homepage"),
        description=metadata.get("description"), uses_lx_global=uses_lx_global,
        events=tuple(sorted(events)), endpoints=tuple(sorted(endpoints)),
        referenced_hosts=tuple(sorted(referenced)), schemes=tuple(sorted(schemes)),
        methods=tuple(sorted(methods)), dynamic_endpoints=tuple(sorted(dynamic)),
        blockers=tuple(blockers), caveats=tuple(caveats))


def analyze_file(path: str | Path) -> LxAnalysis:
    location = Path(path)
    return analyze_source(location.read_text(encoding="utf-8", errors="surrogateescape"),
                          path=str(location))
