"""The main-process search half: what it reads, what it refuses, who it names.

Every payload below is a trimmed copy of a live answer captured on 2026-09-17
from the endpoint the module builds, so the parsers are pinned to bytes the
platforms actually sent rather than to a remembered shape.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from musicdl.contracts.plugin import HttpAction, HttpObservation
from musicdl.plugins.broker import ActionDenied, HttpsActionBroker
from musicdl.sources.lx.analyzer import lx_shaped_file
from musicdl.sources.models import Candidate
from musicdl.sources.platform_search import (
    ITEM_PREFIX, PLATFORMS, SEARCH_HOSTS, LxSearchAdapter, PlatformSearch, PlatformSearchError,
    decode_body, parse_hits,
)
from musicdl.sources.registry import SourceEntry, SourceRegistry
from musicdl.sources.search import search_sources

# Kuwo answers a JavaScript object literal, an excerpt first, and HTML entities
# inside the fields.
KW_BODY = (
    "{'abslist':["
    "{'MUSICRID':'MUSIC_143849264','DC_TARGETID':'143849264','NAME':'夜曲&nbsp;(mp3.2)',"
    "'ARTIST':'周杰伦','ALBUM':'','DURATION':'22'},"
    "{'MUSICRID':'MUSIC_51449297','DC_TARGETID':'51449297','NAME':'夜曲','ARTIST':'华语群星',"
    "'ALBUM':'钢琴心情101','DURATION':'253'},"
    "{'MUSICRID':'MUSIC_7','NAME':'Don\\u0027t Stop','ARTIST':'X','ALBUM':'','DURATION':'200'}"
    "]}"
)

WY_PAYLOAD = {
    "code": 200,
    "result": {"songCount": 332, "songs": [{
        "id": 2725685941, "name": "夜曲", "duration": 233956, "fee": 1,
        "artists": [{"id": 98459986, "name": "Xai小爱"}], "album": {"id": 278269102, "name": "夜曲"},
    }]},
}

TX_PAYLOAD = {"code": 0, "data": {"song": {"curnum": 1, "list": [{
    "songmid": "001zMQr71F1Qo8", "songname": "夜曲", "interval": 226, "albumname": "十一月的萧邦",
    "singer": [{"id": 4558, "name": "周杰伦"}],
}]}}}

KG_PAYLOAD = {"status": 1, "data": {"total": 480, "lists": [
    {"FileHash": "0824176CC451611E819B3071F951589C", "SongName": "夜曲", "Duration": 225,
     "AlbumName": "", "Singers": [{"id": 3520, "name": "周杰伦"}]},
    {"FileHash": "not-a-hash", "SongName": "夜曲", "Duration": 225, "AlbumName": "",
     "Singers": [{"id": 3520, "name": "周杰伦"}]},
]}}

# The same host's other answer: a well-formed envelope that states no results
# at all for a query it holds hundreds of rows for.
EMPTY_KG_PAYLOAD = {"status": 1, "data": {"total": 0, "lists": []}}


def observation(payload, *, status: int = 200, action_id: str = "search-kw") -> HttpObservation:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return HttpObservation(action_id=action_id, status_code=status,
                           body=base64.b64encode(text.encode()).decode())


class _Broker:
    """A stand-in that records what it was asked and replays one answer each."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def fetch(self, action: HttpAction, policy, *, timeout=None) -> HttpObservation:
        self.calls.append({"action": action, "policy": policy, "timeout": timeout})
        platform = action.action_id.split("-", 1)[1]
        answer = self.answers.get(platform)
        if isinstance(answer, list):
            # A platform the module draws more than once answers with the next
            # scripted reply, and keeps repeating the last one once the script
            # is spent.
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if answer is None:
            raise ActionDenied("dns_error", "no answer scripted")
        if isinstance(answer, BaseException):
            raise answer
        return answer


def search_of(answers=None, **options) -> tuple[PlatformSearch, _Broker]:
    broker = _Broker(answers)
    return PlatformSearch(broker, **options), broker


def test_kuwo_is_read_as_the_object_literal_it_sends():
    hits = parse_hits("kw", json.loads(KW_BODY.replace("'", '"')))

    assert [hit.song_id for hit in hits] == ["51449297", "7"]
    assert hits[0].title == "夜曲"
    assert hits[0].artist == "华语群星"
    assert hits[0].album == "钢琴心情101"
    assert hits[0].duration == 253
    # The escaped apostrophe survives the quoting repair: an apostrophe inside
    # a single-quoted value is escaped by the dialect, so rewriting the
    # delimiter cannot change what the value says.
    assert hits[1].title == "Don't Stop"


def test_kuwo_names_the_id_the_sources_resolve_and_drops_the_excerpt():
    hits = parse_hits("kw", {"abslist": [
        {"MUSICRID": "MUSIC_143849264", "DC_TARGETID": "143849264", "NAME": "夜曲",
         "ARTIST": "周杰伦", "ALBUM": "", "DURATION": "22"},
        {"MUSICRID": "MUSIC_51449297", "NAME": "夜曲&nbsp;", "ARTIST": "华语群星",
         "ALBUM": "钢琴心情101", "DURATION": "253"},
    ]})

    # The 22-second row is a ringtone excerpt of the same song, and the prefix
    # the search API adds is not part of the rid a source resolves.
    assert [hit.song_id for hit in hits] == ["51449297"]
    assert hits[0].title == "夜曲"


# A trimmed copy of the live answer ``search.kuwo.cn`` gave ``笨蛋 汪苏泷`` on
# 2026-09-22.  The one JSON read that decodes the object literal is not the last
# layer: the ``&`` between two names arrives four backslashes deep, so two of
# them survived into the artist and were read by a person as ``\\u0026``.
KW_TWICE_ESCAPED_BODY = (
    "{'abslist':[{'MUSICRID':'MUSIC_359805417','NAME':'明天会更好',"
    "'ARTIST':'汪苏泷\\\\\\\\u0026刘维\\\\\\\\u0026刘宇宁','ALBUM':'','DURATION':'296'}]}"
)


def test_the_escaping_kuwo_leaves_in_its_fields_is_resolved_before_a_person_reads_it():
    body = base64.b64encode(KW_TWICE_ESCAPED_BODY.encode()).decode()
    hits = parse_hits("kw", decode_body(body))

    # The parse itself reads the literal, not the escaping inside it.
    assert hits[0].artist == r"汪苏泷\\u0026刘维\\u0026刘宇宁"
    assert hits[0].title == "明天会更好"
    # A candidate is where the text stops being wire format.
    candidate = Candidate(source_id="lx-jade-pro", source_version="1.2.2",
                          item_id=hits[0].item_id, title=hits[0].title, artist=hits[0].artist,
                          album=hits[0].album, duration=hits[0].duration, platform=hits[0].platform)
    assert candidate.artist == "汪苏泷&刘维&刘宇宁"
    assert "\\" not in candidate.artist


def test_netease_milliseconds_become_seconds_and_the_credit_is_joined():
    hits = parse_hits("wy", WY_PAYLOAD)

    assert len(hits) == 1
    assert hits[0].song_id == "2725685941"
    assert hits[0].duration == 233
    assert hits[0].artist == "Xai小爱"
    assert hits[0].album == "夜曲"


def test_qq_uses_the_songmid_and_the_interval_it_states():
    hits = parse_hits("tx", TX_PAYLOAD)

    assert len(hits) == 1
    assert hits[0].song_id == "001zMQr71F1Qo8"
    assert hits[0].duration == 226
    assert hits[0].artist == "周杰伦"


def test_kugou_keeps_the_file_hash_and_refuses_anything_else_as_one():
    hits = parse_hits("kg", KG_PAYLOAD)

    assert [hit.song_id for hit in hits] == ["0824176CC451611E819B3071F951589C"]
    assert hits[0].title == "夜曲"
    assert hits[0].artist == "周杰伦"


def test_a_row_is_dropped_rather_than_repaired_when_it_cannot_be_one():
    hits = parse_hits("kw", {"abslist": [
        {"MUSICRID": "MUSIC_1:2", "NAME": "夜曲", "ARTIST": "周杰伦", "DURATION": "200"},
        {"MUSICRID": "MUSIC_2", "NAME": "", "ARTIST": "周杰伦", "DURATION": "200"},
        {"MUSICRID": "MUSIC_3", "NAME": "夜曲", "ARTIST": "   ", "DURATION": "200"},
        {"MUSICRID": "MUSIC_4", "NAME": "夜曲", "ARTIST": "周杰伦"},
        {"NAME": "夜曲", "ARTIST": "周杰伦", "DURATION": "200"},
    ]})

    # An id that would change what `lx:kw:<id>` means, an empty field, a
    # missing id: each is dropped. A row that states no duration at all keeps
    # an unknown duration instead of being thrown away.
    assert [(hit.song_id, hit.duration) for hit in hits] == [("4", None)]


def test_an_answer_of_the_wrong_shape_is_nothing_rather_than_an_error():
    assert parse_hits("kw", ["not", "a", "mapping"]) == ()
    assert parse_hits("kw", {"abslist": "nope"}) == ()
    assert parse_hits("wy", {"result": {"songs": None}}) == ()
    assert parse_hits("tx", {}) == ()
    assert parse_hits("mg", {"abslist": []}) == ()


def test_a_platform_that_answers_http_200_with_nonsense_is_unreadable():
    search, _ = search_of({"kw": observation("not json at all", action_id="search-kw")})

    with pytest.raises(PlatformSearchError) as raised:
        asyncio.run(search.hits("kw", "夜曲"))

    assert raised.value.code == "search_unreadable"


def test_one_upstream_search_answers_every_source_in_the_round():
    search, broker = search_of({"kw": observation(KW_BODY)})

    async def round_of_three():
        return await asyncio.gather(*(search.hits("kw", "夜曲") for _ in range(3)))

    answers = asyncio.run(round_of_three())

    assert answers[0] == answers[1] == answers[2]
    assert [hit.song_id for hit in answers[0]] == ["51449297", "7"]
    # Twelve installed sources resolving against the same catalogue must not
    # become twelve upstream searches.
    assert len(broker.calls) == 1


def test_the_shared_answer_is_kept_for_the_round_and_a_new_query_is_a_new_search():
    search, broker = search_of({"kw": observation(KW_BODY)})
    asyncio.run(search.hits("kw", "夜曲"))
    asyncio.run(search.hits("kw", "夜曲"))
    asyncio.run(search.hits("kw", "晴天"))

    assert len(broker.calls) == 2
    assert [call["action"].url for call in broker.calls][:1] == [
        "https://search.kuwo.cn/r.s?all=%E5%A4%9C%E6%9B%B2&ft=music&itemset=web_2013&client=kt"
        "&pn=0&rn=20&rformat=json&encoding=utf8"]


def test_each_platform_is_asked_at_its_own_host_with_the_referer_it_requires():
    search, broker = search_of({name: observation({"data": {}}, action_id=f"search-{name}")
                                for name in PLATFORMS})

    async def ask_every_platform():
        return await asyncio.gather(*(search.hits(name, "q") for name in PLATFORMS))

    asyncio.run(ask_every_platform())

    asked = {call["action"].action_id: call["action"] for call in broker.calls}
    assert sorted(asked) == ["search-kg", "search-kw", "search-tx", "search-wy"]
    assert {url.split("/")[2] for url in (call["action"].url for call in broker.calls)} == set(SEARCH_HOSTS)
    assert asked["search-wy"].headers == {"Referer": "https://music.163.com/"}
    assert asked["search-tx"].headers == {"Referer": "https://y.qq.com/"}
    assert asked["search-kg"].headers == {"Referer": "https://www.kugou.com/"}
    assert asked["search-kw"].headers == {"Referer": "https://www.kuwo.cn/"}


def test_netease_is_asked_at_the_endpoint_that_still_answers_the_documented_envelope():
    # Measured 2026-09-20: ``/api/search/get/web`` answers a 36 KiB body whose
    # ``result`` is one hex string -- an encrypted envelope every wy column
    # came back empty from -- while ``/api/search/get`` still answers
    # ``{"result": {"songs": [...]}}`` and parsed twenty rows for one query.
    # The suffix is the whole difference, so the URL is pinned here.
    search, broker = search_of({"wy": observation(WY_PAYLOAD)})

    hits = asyncio.run(search.hits("wy", "夜曲"))

    assert broker.calls[0]["action"].url == (
        "https://music.163.com/api/search/get?s=%E5%A4%9C%E6%9B%B2&type=1&offset=0&limit=20")
    assert [hit.song_id for hit in hits] == ["2725685941"]


def test_the_search_policy_is_the_four_hosts_and_a_bare_policy_object():
    search, broker = search_of({"kw": observation(KW_BODY)})
    asyncio.run(search.hits("kw", "夜曲"))

    assert broker.calls[0]["policy"] == SEARCH_HOSTS
    assert broker.calls[0]["timeout"] == search.timeout


def test_the_platform_that_answers_empty_by_accident_is_drawn_again():
    # Measured 2026-09-20: kugou's search host answers ``status: 1`` with
    # ``total: 0`` and no rows for about half the queries it holds results for,
    # so one empty envelope from it is not a finding about the catalogue.
    search, broker = search_of({"kg": [observation(EMPTY_KG_PAYLOAD, action_id="search-kg"),
                                       observation(KG_PAYLOAD, action_id="search-kg")]})

    hits = asyncio.run(search.hits("kg", "夜曲"))

    assert [hit.song_id for hit in hits] == ["0824176CC451611E819B3071F951589C"]
    assert len(broker.calls) == 2
    assert {call["action"].url for call in broker.calls} == {
        "https://songsearch.kugou.com/song_search_v2?keyword=%E5%A4%9C%E6%9B%B2&page=1&pagesize=20"}


def test_a_query_every_draw_of_which_is_empty_is_empty_rather_than_a_failure():
    search, broker = search_of({"kg": observation(EMPTY_KG_PAYLOAD, action_id="search-kg")})

    hits = asyncio.run(search.hits("kg", "no such song"))

    assert hits == ()
    assert len(broker.calls) == 3


def test_a_draw_that_runs_out_of_time_leaves_the_next_one_to_answer():
    search, broker = search_of({"kg": [ActionDenied("timeout", "action timed out"),
                                       observation(KG_PAYLOAD, action_id="search-kg")]})

    hits = asyncio.run(search.hits("kg", "夜曲"))

    assert [hit.song_id for hit in hits] == ["0824176CC451611E819B3071F951589C"]
    assert len(broker.calls) == 2


def test_a_platform_that_answers_what_it_was_asked_is_drawn_once():
    # Kuwo, NetEase, and QQ answered six of six queries and never answered
    # empty, so a second request there would be load on the upstream and
    # nothing else.  Kuwo answering an empty catalogue is a real answer.
    search, broker = search_of({"kw": observation({"abslist": []})})

    hits = asyncio.run(search.hits("kw", "no such song"))

    assert hits == ()
    assert len(broker.calls) == 1


def test_every_draw_of_one_platform_stays_inside_the_platforms_own_budget():
    search, broker = search_of({"kg": [ActionDenied("timeout", "action timed out"),
                                       ActionDenied("timeout", "action timed out"),
                                       observation(KG_PAYLOAD, action_id="search-kg")]}, timeout=6.0)

    asyncio.run(search.hits("kg", "夜曲"))

    # Three draws split one six-second budget rather than taking six seconds
    # each, so the platform cannot outlive what its caller allowed it.
    assert [call["timeout"] for call in broker.calls] == pytest.approx([2.0, 3.0, 6.0], abs=0.05)


def test_the_real_broker_refuses_a_host_or_a_scheme_the_search_never_uses():
    broker = HttpsActionBroker()

    with pytest.raises(ActionDenied) as host:
        broker.fetch(HttpAction(action_id="x", url="https://example.com/"), SEARCH_HOSTS)
    with pytest.raises(ActionDenied) as scheme:
        broker.fetch(HttpAction(action_id="x", url="http://search.kuwo.cn/r.s"), SEARCH_HOSTS)
    with pytest.raises(ActionDenied) as port:
        broker.fetch(HttpAction(action_id="x", url="https://search.kuwo.cn:8443/r.s"), SEARCH_HOSTS)

    assert (host.value.code, scheme.value.code, port.value.code) == (
        "host_denied", "scheme_denied", "port_denied")


def test_a_denied_platform_reports_the_denial_it_was_given():
    search, _ = search_of({"kw": ActionDenied("host_denied", "not this host")})

    with pytest.raises(PlatformSearchError) as raised:
        asyncio.run(search.hits("kw", "夜曲"))

    assert raised.value.code == "search_host_denied"


def test_an_http_error_status_is_a_failure_not_an_empty_success():
    search, _ = search_of({"kw": observation({"abslist": []}, status=502)})

    with pytest.raises(PlatformSearchError) as raised:
        asyncio.run(search.hits("kw", "夜曲"))

    assert raised.value.code == "search_failed"


def test_the_adapter_names_the_source_that_will_resolve_each_hit():
    search, _ = search_of({"kw": observation(KW_BODY)})
    adapter = LxSearchAdapter("lx-jade-pro", "1.2.2", search, platforms=("kw",))

    candidates = asyncio.run(adapter.search("夜曲"))

    assert [candidate.item_id for candidate in candidates] == [
        f"{ITEM_PREFIX}kw:51449297", f"{ITEM_PREFIX}kw:7"]
    assert {candidate.source_id for candidate in candidates} == {"lx-jade-pro"}
    assert {candidate.source_version for candidate in candidates} == {"1.2.2"}
    # Which catalogue the row came from travels with it: twelve channels answer
    # from the same one, so without this a kw row and a kw row on another
    # channel are the same row with the same words.
    assert {candidate.platform for candidate in candidates} == {"kw"}
    # Nothing here claims a container the download has not been asked for yet.
    assert {candidate.format for candidate in candidates} == {None}
    assert {candidate.bitrate for candidate in candidates} == {None}


def test_one_silent_platform_still_returns_what_the_others_answered():
    search, _ = search_of({"kw": observation(KW_BODY)})
    adapter = LxSearchAdapter("lx-jade", "1", search, platforms=("kw", "wy", "tx", "kg"))

    candidates = asyncio.run(adapter.search("夜曲"))

    assert [candidate.item_id for candidate in candidates] == [
        f"{ITEM_PREFIX}kw:51449297", f"{ITEM_PREFIX}kw:7"]


def test_a_search_no_platform_answered_is_a_failure_not_an_empty_list():
    search, _ = search_of({"kw": ActionDenied("dns_error", "unresolved"),
                           "wy": ActionDenied("dns_error", "unresolved")})
    adapter = LxSearchAdapter("lx-jade", "1", search, platforms=("kw", "wy"))

    with pytest.raises(PlatformSearchError):
        asyncio.run(adapter.search("夜曲"))


def test_the_adapter_output_survives_the_search_pipeline():
    search, _ = search_of({"kw": observation(KW_BODY)})
    adapter = LxSearchAdapter("lx-jade", "1.2.2", search, platforms=("kw",))
    registry = SourceRegistry([SourceEntry("lx-jade", "1.2.2", adapter)])

    result = asyncio.run(search_sources(registry, "夜曲"))

    assert result.statuses[0].status == "ok"
    assert [(candidate.source_id, candidate.item_id) for candidate in result.candidates] == [
        ("lx-jade", "lx:kw:51449297"), ("lx-jade", "lx:kw:7")]


def test_an_adapter_refuses_the_query_the_pipeline_would_have_refused():
    search, _ = search_of({})
    adapter = LxSearchAdapter("lx-jade", "1", search, platforms=("kw",))

    with pytest.raises(ValueError):
        asyncio.run(adapter.search("   "))
    with pytest.raises(ValueError):
        asyncio.run(adapter.search("x" * 501))


def test_a_stored_script_is_recognised_as_an_lx_source_by_reading_it(tmp_path):
    source = tmp_path / "source.js"
    source.write_text("const { send } = globalThis.lx;\nsend(1, {});\n", encoding="utf-8")
    plain = tmp_path / "plain.js"
    plain.write_text("export const answer = 42;\n", encoding="utf-8")

    assert lx_shaped_file(source) is True
    assert lx_shaped_file(plain) is False
    assert lx_shaped_file(tmp_path / "missing.js") is False
