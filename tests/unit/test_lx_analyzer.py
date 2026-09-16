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
    assert sorted(item.code for item in analysis.caveats) == ["dynamic_endpoint", "literal_hosts_only",
                                                              "resolved_host_unknown"]
    # The domain table is the allowlist, and anything else fails closed in the broker.
    assert analysis.allowed_hosts == ("music.example.com",)


def test_plain_http_endpoint_becomes_a_grant_rather_than_a_refusal():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'http://music.example.com/api';\non(EVENT_NAMES.request, () => U);\n"))

    assert analysis.verdict == "installable"
    assert "plain_http_endpoint" in [item.code for item in analysis.caveats]
    assert "http" in analysis.schemes and "https" not in analysis.schemes
    # The policy decides, so the requirement is named instead of a refusal.
    # Open egress comes with every lx source, because the media URL it returns
    # cannot be allowlisted from the script text; see the analyzer's grants.
    assert analysis.required_grants == {"allow_insecure_http": True, "allow_any_host": True}


def test_an_address_literal_becomes_a_grant():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'https://103.79.184.97/api';\non(EVENT_NAMES.request, () => U);\n"))

    assert analysis.verdict == "installable" and analysis.blocked_reasons == ()
    assert analysis.ip_hosts == ("103.79.184.97",)
    assert analysis.required_grants == {"allow_ip_hosts": True, "allow_any_host": True}


def test_a_method_the_broker_cannot_perform_is_refused():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'https://music.example.com/api';\n"
                                  "on(EVENT_NAMES.request, () => ({ url: U, method: 'PUT' }));\n"))

    assert analysis.blocked_reasons == ("unsupported_method",)
    assert analysis.methods == ("put",)


def test_post_is_supported_and_a_source_local_helper_name_is_not_a_verb():
    analysis = analyze_source(_lx("const { EVENT_NAMES, request, on } = globalThis.lx;\n"
                                  "const U = 'https://music.example.com/api';\n"
                                  "const opts = { method: 'POST' }, helper = { method: 'CGIGETVKEY' };\n"
                                  "on(EVENT_NAMES.request, () => request(U, opts, () => helper));\n"))

    assert analysis.verdict == "installable"
    assert analysis.methods == ("cgigetvkey", "post")


def test_a_non_default_port_becomes_a_grant():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const U = 'http://175.27.166.236:8928/kwstream';\n"
                                  "on(EVENT_NAMES.request, () => U);\n"))

    assert analysis.verdict == "installable"
    assert analysis.ports == (8928,) and analysis.allowed_ports == (443, 8928)
    assert analysis.required_grants["allowed_ports"] == [443, 8928]


def test_an_escaped_string_table_requires_open_egress_instead_of_refusal():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "const T = ['\\x6d\\x75\\x73\\x69\\x63\\x2e'];on(EVENT_NAMES.request, () => T);\n"))

    assert analysis.verdict == "installable" and analysis.blocked_reasons == ()
    assert "opaque_script" in [item.code for item in analysis.caveats]
    assert analysis.opaque is True and analysis.lx_shaped is True
    assert analysis.required_grants == {"allow_any_host": True}


def test_a_readable_source_with_no_endpoint_also_requires_open_egress():
    analysis = analyze_source(_lx("const { EVENT_NAMES, on } = globalThis.lx;\n"
                                  "on(EVENT_NAMES.request, () => resolve(info));\n"))

    assert analysis.verdict == "installable" and analysis.open_egress is True
    assert analysis.required_grants == {"allow_any_host": True}


def test_an_oversized_script_is_refused_before_anything_else():
    analysis = analyze_source(_lx("// padding\n" + "x" * MAX_SOURCE_BYTES))

    assert analysis.blocked_reasons[0] == "source_too_large"
    assert analysis.size_bytes > MAX_SOURCE_BYTES


def test_a_script_that_is_not_an_lx_source_is_refused():
    analysis = analyze_source("module.exports = () => 'https://music.example.com';\n")

    assert analysis.blocked_reasons == ("unsupported_shape",)
    assert analysis.lx_shaped is False


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
