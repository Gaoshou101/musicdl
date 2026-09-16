"""Static capability analysis for lx-music custom source scripts.

The broker enforces a per-source egress policy.  Its strict default is HTTPS to
named DNS hosts on port 443, and every widening -- plain HTTP, an address
literal, a non-standard port, or "any host at all" -- is a separate operator
decision recorded on the manifest.

This module decides which of those decisions a script *needs*, and refuses only
what no policy could make work.  The split matters: "the script builds an
``http://`` URL" is a policy question the operator can answer, while "the script
calls ``method: 'PUT'``" is not, because the broker performs GET and POST only.
A refusal always names the text that produced it, and a requirement always names
the opt-in that satisfies it, so an install either succeeds or explains itself.

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
from musicdl.contracts.plugin import (
    DEFAULT_EGRESS_PORT, SUPPORTED_HTTP_METHODS, literal_address,
)

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

# The broker implements GET and POST.  A ``method`` property holding any *other*
# registered HTTP verb is a refusal, because no policy setting can satisfy it.
# A name outside the registry is not an HTTP verb at all -- sources spell their
# own helpers ``CGIGETVKEY`` -- so it is not treated as one.
_SUPPORTED_METHODS = frozenset(name.lower() for name in SUPPORTED_HTTP_METHODS)
_UNSUPPORTED_HTTP_METHODS = frozenset({"put", "delete", "patch", "head", "options", "trace", "connect"})
_AUTHORITY_PREFIX = re.compile(r"^[A-Za-z0-9.:\-\[\]%~]*")


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
    ip_hosts: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    insecure_http: bool = False
    # Whether the endpoints this script *calls* could not be derived at all (an
    # opaque script, or one with no readable URL).  Distinct from the
    # ``allow_any_host`` requirement every lx source carries: the host in the
    # media URL is produced while the script runs, so no analysis can see it.
    open_egress: bool = False
    opaque: bool = False
    blockers: tuple[LxFinding, ...] = ()
    caveats: tuple[LxFinding, ...] = ()

    @property
    def lx_shaped(self) -> bool:
        """Whether this is an lx custom source, readable or not.

        An escaped string table hides the ``globalThis.lx`` reference itself, so
        an unreadable script is routed as a candidate lx source rather than as
        an unrelated module.  ``opaque`` marks exactly that case.
        """
        return self.uses_lx_global or bool(self.events) or self.opaque

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

    @property
    def allowed_ports(self) -> tuple[int, ...]:
        """The ports the manifest must list for this script to reach anything."""
        return tuple(sorted({DEFAULT_EGRESS_PORT, *self.ports}))

    @property
    def required_grants(self) -> dict[str, Any]:
        """The manifest opt-ins this script needs, and nothing it does not.

        The portal must echo these back before the install succeeds, so a
        widened policy is always one deliberate operator decision per source
        rather than a side effect of importing a file.
        """
        grants: dict[str, Any] = {}
        if self.insecure_http:
            grants["allow_insecure_http"] = True
        if self.ip_hosts:
            grants["allow_ip_hosts"] = True
        # Every lx source gets open egress, not only the ones whose endpoints
        # this analysis cannot derive.  The endpoints a script calls are
        # allowlistable; the media URL it hands back is not, because that host
        # is produced while the script runs.  Measured on 2026-09-16: the
        # analysed aggregate source resolved kw/128014 to a kuwo CDN link
        # (kw-er.kuwo.cn) its own text never mentions, and the media transport,
        # which shares this manifest's policy, then failed the download with
        # media_host_denied after a resolve that had succeeded.  Requiring the
        # grant here makes that one decision explicit per source -- the portal
        # refuses the install without it -- instead of leaving the operator to
        # discover it as a download that never finishes.
        grants["allow_any_host"] = True
        if self.ports:
            grants["allowed_ports"] = list(self.allowed_ports)
        return grants

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "author": self.author,
                "license": self.license, "homepage": self.homepage,
                "description": self.description, "sha256": self.sha256,
                "size_bytes": self.size_bytes, "path": self.path, "verdict": self.verdict,
                "events": list(self.events), "endpoints": list(self.endpoints),
                "referenced_hosts": list(self.referenced_hosts),
                "allowed_hosts": list(self.allowed_hosts), "schemes": list(self.schemes),
                "methods": list(self.methods), "dynamic_endpoints": list(self.dynamic_endpoints),
                "ip_hosts": list(self.ip_hosts), "allowed_ports": list(self.allowed_ports),
                "insecure_http": self.insecure_http, "open_egress": self.open_egress,
                "opaque": self.opaque,
                "required_grants": self.required_grants,
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


def _host_and_port(rest: str) -> tuple[str, str, bool]:
    """Return ``(host, port, dynamic)`` for the authority of an absolute URL.

    A URL inside prose runs straight on into the next word -- one analysed
    source writes ``//去http://api-v2.yuafeng.cn注册`` -- so the authority ends
    at the first character a host cannot contain.
    """
    raw = re.split(r"[/?#]", rest, maxsplit=1)[0].split("@")[-1]
    authority = _AUTHORITY_PREFIX.match(raw).group(0)
    if authority.startswith("["):
        host, _, remainder = authority.partition("]")
        host, port = host + "]", remainder[1:] if remainder.startswith(":") else ""
    else:
        host, _, port = authority.rpartition(":")
        if not host:
            host, port = authority, ""
    dynamic = not host or any(char in host for char in _DYNAMIC_HOST_CHARS)
    return host.lower(), port, dynamic


def _classify_host(host: str) -> LxFinding | None:
    """Return the refusal a named host deserves, or ``None`` when it is usable.

    An address literal is deliberately absent: it is an exact target the policy
    can allow, so ``allow_ip_hosts`` answers for it rather than a refusal.
    """
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
    ports: set[int] = set()
    dynamic_ports: set[str] = set()
    for match in _URL.finditer(code):
        schemes.add(match.group("scheme").lower())
        host, port, is_dynamic = _host_and_port(match.group("rest"))
        if is_dynamic:
            dynamic.add(match.group(0))
            continue
        if port:
            if port.isdigit() and 0 < int(port) <= 65535:
                ports.add(int(port))
            else:
                dynamic_ports.add(port)
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
    obfuscated = bool(_ESCAPED.search(code))
    if not uses_lx_global and not events and not obfuscated:
        blockers.append(LxFinding("unsupported_shape",
                                  "script neither reads globalThis.lx nor registers an lx event handler"))
    if obfuscated:
        # Escaped code points hide every endpoint.  That is no longer a refusal,
        # because the policy can carry the one grant that makes it work; it is
        # the reason the grant is required.
        caveats.append(LxFinding("opaque_script",
                                 "string table is stored as escaped code points, so no endpoint can be derived"))
    insecure_http = "http" in schemes
    if insecure_http:
        caveats.append(LxFinding("plain_http_endpoint",
                                 "script builds plain http:// URLs and needs the allow_insecure_http grant"))
    for host in sorted(endpoints):
        if literal_address(host.strip("[]")) is not None:
            continue
        finding = _classify_host(host)
        if finding is not None:
            blockers.append(finding)
    ip_hosts = tuple(sorted(host for host in endpoints if literal_address(host.strip("[]")) is not None))
    if ip_hosts:
        caveats.append(LxFinding("ip_literal_endpoint",
                                 "script names addresses rather than names and needs the allow_ip_hosts grant"))
    for method in sorted(methods & _UNSUPPORTED_HTTP_METHODS):
        blockers.append(LxFinding("unsupported_method",
                                  f"script selects method {method.upper()!r}; the broker performs GET and POST"))
    if ports:
        caveats.append(LxFinding("explicit_port",
                                 f"script names port(s) {sorted(ports)} and needs them on allowed_ports"))
    for value in sorted(dynamic_ports):
        caveats.append(LxFinding("dynamic_port",
                                 f"port {value!r} is not a fixed number this analysis can allowlist"))
    for value in sorted(dynamic):
        # Not a refusal: the broker only ever reaches hosts on the derived
        # allowlist, so an unlisted host fails closed at request time.
        caveats.append(LxFinding("dynamic_endpoint",
                                 f"endpoint {value!r} is assembled at runtime from other values"))
    if not endpoints and referenced:
        caveats.append(LxFinding("literal_hosts_only",
                                 "every endpoint comes from bare domain literals rather than absolute URLs"))
    caveats.append(LxFinding("resolved_host_unknown",
                             "the media URL this script returns is assembled while it runs, so the host it "
                             "points at cannot be allowlisted and the source always needs open egress"))
    open_egress = obfuscated or (not endpoints and not referenced)
    if not endpoints and not referenced and not obfuscated:
        caveats.append(LxFinding("undecidable_endpoints",
                                 "script declares no endpoint this analysis can derive and needs the allow_any_host grant"))

    return LxAnalysis(
        sha256=hashlib.sha256(source_bytes).hexdigest(), size_bytes=len(source_bytes), path=path,
        name=metadata.get("name"), version=metadata.get("version"), author=metadata.get("author"),
        license=metadata.get("license"), homepage=metadata.get("homepage"),
        description=metadata.get("description"), uses_lx_global=uses_lx_global,
        events=tuple(sorted(events)), endpoints=tuple(sorted(endpoints)),
        referenced_hosts=tuple(sorted(referenced)), schemes=tuple(sorted(schemes)),
        methods=tuple(sorted(methods)), dynamic_endpoints=tuple(sorted(dynamic)),
        ip_hosts=ip_hosts, ports=tuple(sorted(ports)), insecure_http=insecure_http,
        open_egress=open_egress, opaque=obfuscated,
        blockers=tuple(blockers), caveats=tuple(caveats))


def analyze_file(path: str | Path) -> LxAnalysis:
    location = Path(path)
    return analyze_source(location.read_text(encoding="utf-8", errors="surrogateescape"),
                          path=str(location))
