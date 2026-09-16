from musicdl.contracts import MAX_SOURCE_BYTES
from musicdl.sources.lx.analyzer import analyze_source, strip_leading_comments


def _lx(body: str, header: str = "* @name Demo\n * @version 1.0.0\n * @homepage https://prose.example.com/") -> str:
    return f"/*\n{header}\n */\n{body}\n"


LX_BODY = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
const API = 'https://music.example.com/api';
on(EVENT_NAMES.request, ({ action, info }) => {
  return request(`${API}/search?q=${info.keyword}`, { method: 'GET' });
});
send(EVENT_NAMES.inited, { status: true, sources: { kw: { name: 'KW' } } });
"""


def test_analysis_reads_the_header_without_trusting_its_hosts():
    analysis = analyze_source(_lx(LX_BODY))

    assert (analysis.name, analysis.version) == ("Demo", "1.0.0")
    assert analysis.homepage == "https://prose.example.com/"
    assert analysis.uses_lx_global is True and analysis.events == ("inited", "request")
    # Prose names a host the script never contacts; it must not enter the allowlist.
    assert analysis.allowed_hosts == ("music.example.com",)
    assert analysis.verdict == "installable" and analysis.blockers == ()


def test_an_endpoint_assembled_at_runtime_is_a_caveat_not_a_refusal():
    source = _lx("const { EVENT_NAMES, on } = globalThis.lx;\nconst H = 'music.example.com';\n"
                 "const U = `https://${H}/api`;\non(EVENT_NAMES.request, () => U);\n")
    analysis = analyze_source(source)

    assert analysis.verdict == "installable"
    assert sorted(item.code for item in analysis.caveats) == ["dynamic_endpoint", "literal_hosts_only"]
    # The domain table is the allowlist, and anything else fails closed in the broker.
    assert analysis.allowed_hosts == ("music.example.com",)


def test_plain_http_endpoint_is_refused_with_the_url_as_evidence():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'http://music.example.com/api';\non(EVENT_NAMES.request, () => U);\n"))

    assert analysis.verdict == "blocked" and "plain_http" in analysis.blocked_reasons
    assert "https" not in analysis.schemes


def test_ip_literal_endpoint_has_no_name_to_pin():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'https://103.79.184.97/api';\non(EVENT_NAMES.request, () => U);\n"))

    assert analysis.blocked_reasons == ("ip_literal_host",)


def test_a_method_the_broker_cannot_perform_is_refused():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'https://music.example.com/api';\n"
                                  "on(EVENT_NAMES.request, () => ({ url: U, method: 'POST' }));\n"))

    assert analysis.blocked_reasons == ("unsupported_method",)
    assert analysis.methods == ("post",)


def test_an_escaped_string_table_is_refused_because_no_endpoint_can_be_derived():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const T = ['\\x6d\\x75\\x73\\x69\\x63\\x2e'];on(EVENT_NAMES.request, () => T);\n"))

    assert "obfuscated_strings" in analysis.blocked_reasons
    assert analysis.blocked_reasons[-1] == "no_endpoints"


def test_an_oversized_script_is_refused_before_anything_else():
    analysis = analyze_source(_lx("// padding\n" + "x" * MAX_SOURCE_BYTES))

    assert analysis.blocked_reasons[0] == "source_too_large"
    assert analysis.size_bytes > MAX_SOURCE_BYTES


def test_a_script_that_is_not_an_lx_source_is_refused():
    analysis = analyze_source("module.exports = () => 'https://music.example.com';\n")

    assert analysis.blocked_reasons == ("unsupported_shape",)


def test_version_like_literals_are_not_mistaken_for_hosts():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const V = 'v3.2.11', B = '0.0.0.0', H = 'yy.zddyr.top';\n"
                                  "on(EVENT_NAMES.request, () => `https://${H}/api`);\n"))

    assert analysis.allowed_hosts == ("yy.zddyr.top",)


def test_only_the_leading_comment_is_removed():
    body = "const A = '/* not a comment */';\nconst B = 'https://music.example.com/';\n"
    source = f"/* header */\n{body}"

    stripped = strip_leading_comments(source)

    assert stripped == body
    assert "not a comment" in stripped
