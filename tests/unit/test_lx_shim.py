"""The lx custom-source adapter: the replay machine, and both operation maps.

These run the real host, so they only execute when a Deno binary is on PATH.
The assertions are about the adapter's own contract -- which request becomes a
step's action, what a replayed body turns into, and which candidate maps back to
which `musicInfo` -- because that is the part a long-lived browser host does for
free and a one-process-per-step host has to earn.
"""
import asyncio
import base64
import hashlib
import json
import shutil
import time
from uuid import uuid4

import pytest

from musicdl.contracts.plugin import (
    HttpAction,
    HttpObservation,
    PluginInvocation,
    PluginManifest,
    PluginRequest,
)
from musicdl_plugin_runner.supervisor import Supervisor, build_host_command

OPERATIONS = ("search", "resolve")
SEARCH_URL = "https://api.zhihu.example/search?keywords=hello"
requires_deno = pytest.mark.skipif(shutil.which("deno") is None, reason="Deno executable unavailable")

# A source shaped like a real one: it registers a handler at the top level, sends
# its `inited` metadata, and never declares a `handle`.
HANDLER_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
function get(url, options) {
  return new Promise((resolve, reject) => {
    request(url, options, (error, response) => error ? reject(error) : resolve(response));
  });
}
on(EVENT_NAMES.request, async ({ action, info }) => {
  if (action === "musicSearch" || action === "search") {
    const response = await get("https://api.zhihu.example/search?keywords=" + encodeURIComponent(info.keyword), { method: "GET" });
    return { isEnd: true, list: JSON.parse(response.body).list };
  }
  if (action === "musicUrl") return "https://cdn.zhihu.example/" + info.musicInfo.id + "/song.mp3";
  throw new Error("action not supported");
});
send(EVENT_NAMES.inited, { status: true, sources: { qsvip: { name: "汽水VIP", type: "music", actions: ["musicSearch", "musicUrl"], qualitys: ["320k"] } } });
"""

RESOLVE_ONLY_SOURCE = """
const { EVENT_NAMES, on, send } = globalThis.lx;
on(EVENT_NAMES.request, async () => "https://cdn.zhihu.example/1/song.mp3");
send(EVENT_NAMES.inited, { status: true, sources: { kg: { name: "酷狗", type: "music", actions: ["musicUrl"], qualitys: ["320k"] } } });
"""

PLAIN_JAVASCRIPT_SOURCE = "const answer = 1 + 1;"

CANDIDATE = {
    "source_id": "qsvip",
    "source_version": "1",
    "item_id": "lx:qsvip:42",
    "title": "晴天",
    "artist": "周杰伦",
    "album": "叶惠美",
    "duration": 269,
    "bitrate": None,
    "format": None,
    "size": None,
}


def _invocation(source, *, operation="search", payload=None, actions=(), observations=()):
    return PluginInvocation(
        manifest=PluginManifest(plugin_id="qsvip", version="1", language="javascript",
                                operations=OPERATIONS, sha256=hashlib.sha256(source.encode()).hexdigest()),
        source=source,
        request=PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation=operation,
                              payload={} if payload is None else payload),
        actions=tuple(actions), observations=tuple(observations))


def _run(invocation):
    return asyncio.run(Supervisor(build_host_command).execute(invocation))


def _observation(body, *, action_id="lx-0", status_code=200, content_type="application/json"):
    encoded = base64.b64encode(body.encode() if isinstance(body, str) else body).decode("ascii")
    return HttpObservation(action_id=action_id, status_code=status_code,
                           headers={"content-type": content_type}, body=encoded)


def test_adapter_is_embedded_ahead_of_the_host():
    payload = build_host_command(_invocation(HANDLER_SOURCE)).stdin_payload.decode()
    assert "globalThis.lx" in payload and "--allow" not in payload
    assert payload.index("globalThis.lx") < payload.index('Object.defineProperty(globalThis,"Worker"')


@requires_deno
def test_a_javascript_source_without_a_handler_is_still_a_failure():
    # Installing a global `handle` must not turn a plugin that simply has no
    # entry point into one that "runs".
    step = _run(_invocation(PLAIN_JAVASCRIPT_SOURCE))
    assert step.response.ok is False and step.response.error.code == "plugin_error"


@requires_deno
def test_search_reports_its_first_request_as_the_step_action():
    step = _run(_invocation(HANDLER_SOURCE, payload={"query": "hello"}))
    assert step.action is not None
    assert step.action.action_id == "lx-0"
    assert step.action.method == "GET"
    assert step.action.url == SEARCH_URL


@requires_deno
def test_search_maps_replayed_items_and_drops_the_unusable_ones():
    # Shaped like `normalizeSongInfo` in the analysed source: the album arrives
    # as `albumName`, and `duration` is already in seconds.
    body = json.dumps({"list": [
        {"id": "42", "name": "晴天", "singer": "周杰伦", "albumName": "叶惠美", "duration": 269},
        {"songmid": "99", "name": "夜曲", "singer": "周杰伦", "albumName": "", "duration": 0},
        {"vid": "7", "name": "无可用ID"},
        {"id": "8", "singer": "无标题"},
    ]})
    actions = [HttpAction(action_id="lx-0", method="GET", url=SEARCH_URL)]
    step = _run(_invocation(HANDLER_SOURCE, payload={"query": "hello"}, actions=actions,
                            observations=[_observation(body)]))
    assert step.response.ok
    assert step.response.result == [
        {"source_id": "qsvip", "source_version": "1", "item_id": "lx:qsvip:42", "title": "晴天",
         "artist": "周杰伦", "album": "叶惠美", "duration": 269, "bitrate": None, "format": None, "size": None},
        {"source_id": "qsvip", "source_version": "1", "item_id": "lx:qsvip:99", "title": "夜曲",
         "artist": "周杰伦", "album": None, "duration": None, "bitrate": None, "format": None, "size": None},
    ]


@requires_deno
def test_resolve_maps_a_candidate_back_into_the_sources_own_id():
    step = _run(_invocation(HANDLER_SOURCE, operation="resolve", payload={"candidate": CANDIDATE}))
    assert step.response.ok
    assert step.response.result == {"candidate_id": "lx:qsvip:42",
                                    "url": "https://cdn.zhihu.example/42/song.mp3",
                                    "extension": "mp3", "media_type": "audio/mpeg", "declared_size": None}


DIRECT_LINK_SOURCE = """
const { EVENT_NAMES, on, send } = globalThis.lx;
on(EVENT_NAMES.request, async () => "http://music.example/wy/wy.php?type=mp3&id=186016&level=3200000");
send(EVENT_NAMES.inited, { sources: { wy: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_a_direct_link_without_a_suffix_falls_back_to_the_requested_quality():
    # The endpoints these sources fall back to answer with a script path that
    # carries no audio suffix, so the requested quality is the only container
    # the source states.
    candidate = dict(CANDIDATE, item_id="lx:wy:186016", source_id="wy")
    lossy = _run(_invocation(DIRECT_LINK_SOURCE, operation="resolve",
                             payload={"candidate": candidate, "quality": "320k"}))
    assert lossy.response.ok
    assert lossy.response.result["extension"] == "mp3"
    assert lossy.response.result["media_type"] == "audio/mpeg"
    lossless = _run(_invocation(DIRECT_LINK_SOURCE, operation="resolve",
                                payload={"candidate": candidate, "quality": "flac"}))
    assert lossless.response.ok
    assert lossless.response.result["extension"] == "flac"
    assert lossless.response.result["media_type"] == "audio/flac"


AAC_LINK_SOURCE = """
const { EVENT_NAMES, on, send } = globalThis.lx;
on(EVENT_NAMES.request, async () => "http://car-bj.kuwo.cn/1904613985.aac?type=convert_url_with_sign");
send(EVENT_NAMES.inited, { sources: { wy: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_an_aac_link_is_declared_as_the_container_it_actually_carries():
    # Measured 2026-09-17: `car-bj.kuwo.cn/.../1904613985.aac` answers with an
    # ISO base media file whose brands are `M4A `, `mp42`, `isom`.  Declared as
    # a guessed mp3, the transport refused four sources' downloads of a real
    # song as `media_response_invalid`; the suffix names an m4a.  Measured
    # 2026-09-20 the same shape of link answers with raw ADTS instead, which is
    # why this declaration is the better of two guesses: the transport
    # publishes the container the bytes it read announce, not this one.
    candidate = dict(CANDIDATE, item_id="lx:wy:1904613985", source_id="wy")
    step = _run(_invocation(AAC_LINK_SOURCE, operation="resolve",
                            payload={"candidate": candidate, "quality": "320k"}))

    assert step.response.ok
    assert step.response.result["extension"] == "m4a"
    assert step.response.result["media_type"] == "audio/mp4"


@requires_deno
def test_request_post_body_is_base64_and_transport_headers_are_dropped():
    source = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, () => new Promise((resolve) => {
  request("https://api.zhihu.example/token", {
    method: "POST",
    headers: { Host: "evil.example", "Content-Type": "application/json", "X-Token": "t" },
    body: { a: 1 },
  }, () => resolve("https://cdn.zhihu.example/1/song.mp3"));
}));
send(EVENT_NAMES.inited, { sources: { qsvip: { type: "music", actions: ["musicSearch", "musicUrl"] } } });
"""
    step = _run(_invocation(source, operation="resolve", payload={"candidate": CANDIDATE}))
    assert step.action is not None and step.action.method == "POST"
    assert step.action.headers == {"Content-Type": "application/json", "X-Token": "t"}
    assert base64.b64decode(step.action.body) == b'{"a":1}'


@requires_deno
def test_a_replayed_call_is_answered_by_its_ordinal_even_when_its_url_moved():
    # The source is re-read before every step, and 玉宁熙-Pro rebuilds a random
    # `user`/`loginUid` query pair on every run, so the same ordinal asks for a
    # slightly different URL on the re-run.  The answer recorded for that
    # ordinal was fetched under this source's own policy, one step ago, and is
    # the answer a live host would have delivered; refusing the moved URL made
    # a working source unresolvable.
    elsewhere = [HttpAction(action_id="lx-0", method="GET",
                            url="https://api.zhihu.example/search?keywords=other")]
    body = json.dumps({"list": [{"id": "42", "name": "晴天", "singer": "周杰伦", "duration": 269}]})
    step = _run(_invocation(HANDLER_SOURCE, payload={"query": "hello"}, actions=elsewhere,
                            observations=[_observation(body)]))
    assert step.response.ok, step.response.error
    assert [item["item_id"] for item in step.response.result] == ["lx:qsvip:42"]


@requires_deno
def test_a_resolve_only_source_refuses_search_instead_of_answering_empty():
    step = _run(_invocation(RESOLVE_ONLY_SOURCE, payload={"query": "hello"}))
    assert step.response.ok is False and step.response.error.code == "plugin_error"


FANOUT_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
function get(url) {
  return new Promise((resolve, reject) => {
    request(url, { method: "GET" }, (error, response) => error ? reject(error) : resolve(response));
  });
}
on(EVENT_NAMES.request, async () => {
  const results = await Promise.allSettled([
    get("https://api.zhihu.example/a"),
    get("https://api.zhihu.example/b"),
  ]);
  for (const result of results) {
    if (result.status === "fulfilled") return "https://cdn.zhihu.example/" + JSON.parse(result.value.body).id + ".mp3";
  }
  throw new Error("every branch failed");
});
send(EVENT_NAMES.inited, { sources: { kg: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_a_source_that_fans_out_still_yields_one_action_per_step():
    # The analysed aggregate source races three handlers at once.  A step carries
    # one action, so the extra calls stay unanswered in that process and the
    # source is re-run for the next one -- which is what makes a fan-out source
    # finish without the host ever holding two live sockets.
    candidate = dict(CANDIDATE, item_id="lx:kg:7", source_id="kg")
    first = _run(_invocation(FANOUT_SOURCE, operation="resolve", payload={"candidate": candidate}))
    assert first.action is not None and first.action.url == "https://api.zhihu.example/a"
    second = _run(_invocation(FANOUT_SOURCE, operation="resolve", payload={"candidate": candidate},
                              actions=[first.action],
                              observations=[_observation(json.dumps({"id": 5}), action_id=first.action.action_id)]))
    assert second.action is not None and second.action.url == "https://api.zhihu.example/b"
    third = _run(_invocation(FANOUT_SOURCE, operation="resolve", payload={"candidate": candidate},
                             actions=[first.action, second.action],
                             observations=[_observation(json.dumps({"id": 5}), action_id=first.action.action_id),
                                           _observation(json.dumps({"id": 9}), action_id=second.action.action_id)]))
    assert third.response.ok
    assert third.response.result["url"] == "https://cdn.zhihu.example/5.mp3"


SHIPPED_SHAPE_SOURCE = """
console.log("聚合音源 特供版 v3.0 已加载");
const { EVENT_NAMES, request, on, send } = globalThis.lx;
function get(url) {
  return new Promise((resolve, reject) => {
    request(url, { method: "GET" }, (error, response) => error ? reject(error) : resolve(response));
  });
}
const FALLBACKS = [
  "https://api.zhihu.example/gdstudio",
  "https://api.zhihu.example/huibq",
  "https://api.zhihu.example/lingchuan",
];
on(EVENT_NAMES.request, async ({ action }) => {
  if (action !== "musicUrl") throw new Error("action not supported");
  const results = await Promise.allSettled(FALLBACKS.map((url) => get(url)));
  for (const result of results) {
    if (result.status !== "fulfilled") continue;
    const body = JSON.parse(result.value.body);
    if (body.url) return body.url;
  }
  throw new Error("every fallback came back empty");
});
send(EVENT_NAMES.inited, { status: true, sources: { wy: { name: "网易", type: "music", actions: ["musicUrl"], qualitys: ["320k"] } } });
"""


@requires_deno
def test_a_source_with_the_shape_of_a_shipped_one_clears_every_hazard_at_once():
    # Each of these three broke a real source on the first live run, and each is
    # fixed in a different file.  Holding them in one source means a regression
    # in any of them fails here instead of only showing up against an upstream.
    candidate = dict(CANDIDATE, item_id="lx:wy:186016", source_id="wy")
    payload = build_host_command(_invocation(SHIPPED_SHAPE_SOURCE)).stdin_payload.decode()
    # The host's own parameter is not named `request`, so a source may destructure
    # one out of `globalThis.lx`; and the console sink is installed before the
    # source runs, so its load-time `console.log` cannot prepend to stdout.
    # The source text is carried as JSON data and so appears earlier in the
    # payload; what has to hold is evaluation order, so the check is against the
    # `new Function` call that runs the source.
    assert payload.index("globalThis.console=") < payload.index('new Function("musicdlRequest"')

    first = _run(_invocation(SHIPPED_SHAPE_SOURCE, operation="resolve", payload={"candidate": candidate}))
    assert first.action is not None and first.action.url == "https://api.zhihu.example/gdstudio"

    empty = _observation(json.dumps({"url": ""}), action_id=first.action.action_id)
    second = _run(_invocation(SHIPPED_SHAPE_SOURCE, operation="resolve", payload={"candidate": candidate},
                              actions=[first.action], observations=[empty]))
    assert second.action is not None and second.action.url == "https://api.zhihu.example/huibq"

    answered = _observation(json.dumps({"url": "http://music.example/wy/wy.php?type=mp3&id=186016"}),
                            action_id=second.action.action_id)
    # `Promise.allSettled` waits for every branch, so a working fallback in the
    # middle of the race is not enough: the source cannot answer while a later
    # branch is still unanswered, and that branch becomes the next step's action.
    third = _run(_invocation(SHIPPED_SHAPE_SOURCE, operation="resolve", payload={"candidate": candidate},
                             actions=[first.action, second.action], observations=[empty, answered]))
    assert third.action is not None and third.action.url == "https://api.zhihu.example/lingchuan"

    dead = _observation(json.dumps({"url": None}), action_id=third.action.action_id)
    final = _run(_invocation(SHIPPED_SHAPE_SOURCE, operation="resolve", payload={"candidate": candidate},
                             actions=[first.action, second.action, third.action],
                             observations=[empty, answered, dead]))
    assert final.response.ok
    assert final.response.result["url"] == "http://music.example/wy/wy.php?type=mp3&id=186016"
    assert final.response.result["extension"] == "mp3"


DENIAL_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
function get(url) {
  return new Promise((resolve, reject) => {
    request(url, { method: "GET" }, (error, response) => {
      if (error) return reject(error);
      if (!response || response.statusCode >= 400) return reject(new Error("HTTP " + (response && response.statusCode)));
      resolve(response);
    });
  });
}
const BRANCHES = [
  "https://api.zhihu.example/a",
  "https://api.zhihu.example/b",
  "https://api.zhihu.example/c",
];
on(EVENT_NAMES.request, async () => {
  const results = await Promise.allSettled(BRANCHES.map((url) => get(url)));
  for (const result of results) {
    if (result.status !== "fulfilled") continue;
    return JSON.parse(result.value.body).url;
  }
  throw new Error("every branch failed");
});
send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_a_refused_branch_looks_like_a_failed_branch_to_the_source():
    # The main process answers a request the broker would not carry with a 599
    # observation instead of ending the invocation, and this is the property
    # that makes one dead endpoint cost one branch instead of the whole source.
    # The status is the signal the sources already test for, so an unmodified
    # third-party script walks its own chain without knowing about the policy.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    refused = HttpObservation(action_id="lx-0", status_code=599)
    first = _run(_invocation(DENIAL_SOURCE, operation="resolve", payload={"candidate": candidate}))
    assert first.action is not None and first.action.url == "https://api.zhihu.example/a"

    second = _run(_invocation(DENIAL_SOURCE, operation="resolve", payload={"candidate": candidate},
                              actions=[first.action], observations=[refused]))
    assert second.action is not None and second.action.url == "https://api.zhihu.example/b"

    third = _run(_invocation(DENIAL_SOURCE, operation="resolve", payload={"candidate": candidate},
                             actions=[first.action, second.action],
                             observations=[refused, _observation("", action_id="lx-1", status_code=599)]))
    assert third.action is not None and third.action.url == "https://api.zhihu.example/c"

    answered = _observation(json.dumps({"url": "http://music.example/kw/kw.php?type=mp3&id=128014"}),
                            action_id="lx-2")
    final = _run(_invocation(DENIAL_SOURCE, operation="resolve", payload={"candidate": candidate},
                             actions=[first.action, second.action, third.action],
                             observations=[refused,
                                           _observation("", action_id="lx-1", status_code=599),
                                           answered]))
    assert final.response.ok
    assert final.response.result["url"] == "http://music.example/kw/kw.php?type=mp3&id=128014"
    assert final.response.result["extension"] == "mp3"


UTILS_SOURCE = """
const { EVENT_NAMES, on, send, utils } = globalThis.lx;
on(EVENT_NAMES.request, async () => {
  const parts = [
    "md5=" + utils.crypto.md5("abc"),
    "m2=" + utils.crypto.md5(new Uint8Array([0x61, 0x62, 0x63]).buffer),
    "hex=" + utils.buffer.bufToString(new Uint8Array([0, 15, 255]).buffer),
    "b64=" + utils.buffer.bufToString(utils.buffer.stringToBuf("hi", "utf-8"), "base64"),
    "utf8=" + encodeURIComponent(utils.buffer.bufToString(utils.buffer.from("hello \\u4f60\\u597d", "utf-8"), "utf-8")),
    "rt=" + utils.buffer.bufToString(utils.buffer.stringToBuf("00ff10", "hex"), "HEX"),
    "nonce=" + utils.buffer.bufToString(utils.crypto.randomBytes(8), "hex"),
  ];
  return "https://cdn.example/song.mp3?" + parts.join("&");
});
send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
"""


NONCE_PAIR_SOURCE = """
const { EVENT_NAMES, on, send, utils } = globalThis.lx;
on(EVENT_NAMES.request, async () => {
  const first = utils.buffer.bufToString(utils.crypto.randomBytes(8), "hex");
  const second = utils.buffer.bufToString(utils.crypto.randomBytes(8), "hex");
  return "https://cdn.example/song.mp3?a=" + first + "&b=" + second;
});
send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_the_utils_toolkit_answers_the_calls_a_source_makes_directly():
    # These sources do not feature-detect `utils`; they call into it and die
    # inside the source when a member is missing, which is why an empty object
    # made eleven of twelve supplied sources fail before their first request.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    step = _run(_invocation(UTILS_SOURCE, operation="resolve", payload={"candidate": candidate}))

    assert step.response.ok, step.response.error
    digest = hashlib.md5(b"abc").hexdigest()
    url = step.response.result["url"]
    assert url.startswith(
        "https://cdn.example/song.mp3"
        f"?md5={digest}&m2={digest}&hex=000fff&b64={base64.b64encode(b'hi').decode()}"
        "&utf8=hello%20%E4%BD%A0%E5%A5%BD&rt=00ff10&nonce=")
    assert len(url.rsplit("nonce=", 1)[1]) == 16


@requires_deno
def test_a_random_nonce_replays_and_still_advances_within_one_run():
    # Two runs of one step have to agree, or the adapter's own replay check
    # refuses the second one; two calls inside one run must not agree, or a
    # source that signs two requests would sign both with one nonce.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    first = _run(_invocation(UTILS_SOURCE, operation="resolve", payload={"candidate": candidate}))
    second = _run(_invocation(UTILS_SOURCE, operation="resolve", payload={"candidate": candidate}))
    assert first.response.result["url"] == second.response.result["url"]

    advancing = _run(_invocation(NONCE_PAIR_SOURCE, operation="resolve", payload={"candidate": candidate}))
    assert advancing.response.ok, advancing.response.error
    query = advancing.response.result["url"].split("?", 1)[1]
    left, right = (item.split("=", 1)[1] for item in query.split("&"))
    assert len(left) == 16 and len(right) == 16 and left != right


MATH_RANDOM_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, () => new Promise((resolve, reject) => {
  // Shaped like 玉宁熙-Pro: a random `user`/`loginUid` pair is drawn on every
  // run, carried in the request, and echoed back inside the JSON the upstream
  // answers with.  The source compares the two and reports
  // "kuwo parse failed" when they disagree, which is what a fresh draw on the
  // re-run made it do.
  const user = Math.random().toString(36).slice(2);
  const loginUid = Math.random().toString(36).slice(2);
  const url = "https://nmobi.kuwo.example/kuwo?user=" + user + "&loginUid=" + loginUid;
  request(url, { method: "GET" }, (error, response) => {
    if (error) return reject(error);
    const body = JSON.parse(response.body);
    if (body.user !== user) return reject(new Error("kuwo parse failed"));
    resolve(body.url);
  });
}));
send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_a_draw_from_math_random_repeats_across_a_replay_and_still_advances():
    # The step that carries the request has to agree with the step that reads
    # the answer, so the stream repeats per process; a source that draws twice
    # has to see two values, so it advances inside one run.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    first = _run(_invocation(MATH_RANDOM_SOURCE, operation="resolve", payload={"candidate": candidate}))
    assert first.action is not None, first.response.error
    query = dict(item.split("=", 1) for item in first.action.url.split("?", 1)[1].split("&"))
    assert query["user"] and query["loginUid"] and query["user"] != query["loginUid"]

    echoed = _observation(json.dumps({"user": query["user"],
                                      "url": "http://music.example/kw/kw.php?type=mp3&id=128014"}),
                          action_id=first.action.action_id)
    second = _run(_invocation(MATH_RANDOM_SOURCE, operation="resolve", payload={"candidate": candidate},
                              actions=[first.action], observations=[echoed]))
    assert second.response.ok, second.response.error
    assert second.response.result["url"] == "http://music.example/kw/kw.php?type=mp3&id=128014"


JSON_BODY_URL = "https://nmobi.kuwo.example/kuwo?rid=128014"
JSON_BODY = json.dumps({"code": 200, "data": {"url": "http://car-lv.kuwo.example/1/song.mp3"}})

# 玉宁熙-Pro reads the body as an object and never checks its type first.
OBJECT_BODY_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, () => new Promise((resolve, reject) => {
  request("https://nmobi.kuwo.example/kuwo?rid=128014", { method: "GET", timeout: 8000, retry: 1 },
    (error, response) => {
      if (error) return reject(error);
      if (Number(response.body.code) !== 200) return reject(new Error("kuwo parse failed"));
      resolve(response.body.data.url);
    });
}));
send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
"""

# Every readable supplied source guards its own parse and keeps the object.
STRING_BODY_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, () => new Promise((resolve, reject) => {
  request("https://nmobi.kuwo.example/kuwo?rid=128014", { method: "GET" }, (error, response) => {
    if (error) return reject(error);
    let body = response.body;
    if (typeof body === "string") body = JSON.parse(body);
    const text = response.body;
    resolve(body.data.url + "?len=" + text.length + "&brace=" + (text.trim()[0] === "{" ? "yes" : "no"));
  });
}));
send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
"""

ARRAY_BODY_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, () => new Promise((resolve, reject) => {
  request("https://api.zhihu.example/list", { method: "GET" }, (error, response) => {
    if (error) return reject(error);
    const list = Array.isArray(response.body) ? response.body : [];
    const reparsed = JSON.parse(response.body);
    resolve("https://cdn.zhihu.example/" + list[0] + "?n=" + reparsed.length);
  });
}));
send(EVENT_NAMES.inited, { sources: { kg: { type: "music", actions: ["musicUrl"] } } });
"""

HTML_BODY_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, () => new Promise((resolve, reject) => {
  request("https://www.97abc.example/count.php", { method: "GET" }, (error, response) => {
    if (error) return reject(error);
    if (typeof response.body !== "string") return reject(new Error("a non-JSON body is text"));
    resolve("https://cdn.zhihu.example/1/song.mp3?visits=" + response.body.trim().split(" ").pop());
  });
}));
send(EVENT_NAMES.inited, { sources: { kg: { type: "music", actions: ["musicUrl"] } } });
"""


@requires_deno
def test_a_json_body_answers_both_the_parsed_and_the_text_reading():
    # The publisher's host hands back a decoded body for a JSON response, and the
    # supplied sources disagree about which they read: five of them guard with
    # `typeof body === "string" ? JSON.parse(body) : body` and 玉宁熙-Pro reads
    # `response.body.code` with no guard at all.  One body has to serve both, and
    # it still has to answer every string method a source reaches for.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    actions = [HttpAction(action_id="lx-0", method="GET", url=JSON_BODY_URL)]
    observations = [_observation(JSON_BODY)]

    parsed = _run(_invocation(OBJECT_BODY_SOURCE, operation="resolve", payload={"candidate": candidate},
                              actions=actions, observations=observations))
    assert parsed.response.ok, parsed.response.error
    assert parsed.response.result["url"] == "http://car-lv.kuwo.example/1/song.mp3"

    text = _run(_invocation(STRING_BODY_SOURCE, operation="resolve", payload={"candidate": candidate},
                            actions=actions, observations=observations))
    assert text.response.ok, text.response.error
    assert text.response.result["url"] == (f"http://car-lv.kuwo.example/1/song.mp3"
                                           f"?len={len(JSON_BODY)}&brace=yes")


@requires_deno
def test_a_json_array_body_keeps_its_array_identity_and_its_text():
    candidate = dict(CANDIDATE, item_id="lx:kg:9", source_id="kg")
    step = _run(_invocation(ARRAY_BODY_SOURCE, operation="resolve", payload={"candidate": candidate},
                            actions=[HttpAction(action_id="lx-0", method="GET",
                                                url="https://api.zhihu.example/list")],
                            observations=[_observation(json.dumps(["a", "b"]))]))
    assert step.response.ok, step.response.error
    assert step.response.result["url"] == "https://cdn.zhihu.example/a?n=2"


@requires_deno
def test_a_padded_json_body_is_still_read_as_json():
    # Upstreams pad their JSON with a newline often enough that the detection
    # cannot be a check on the first character of the raw body.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    step = _run(_invocation(OBJECT_BODY_SOURCE, operation="resolve", payload={"candidate": candidate},
                            actions=[HttpAction(action_id="lx-0", method="GET", url=JSON_BODY_URL)],
                            observations=[_observation("  \n" + JSON_BODY)]))
    assert step.response.ok, step.response.error
    assert step.response.result["url"] == "http://car-lv.kuwo.example/1/song.mp3"


@requires_deno
def test_a_body_that_is_not_json_stays_a_string():
    # The counter endpoint 玉宁熙-Pro calls at load answers with HTML, and a source
    # that reads it as text must not be handed a proxy it cannot index.
    candidate = dict(CANDIDATE, item_id="lx:kg:1", source_id="kg")
    counter = "\u4eca\u65e5\u8bbf\u95ee\u4eba\u6570: 542\n\u7d2f\u8ba1\u8bbf\u95ee\u6b21\u6570: 6861378\n"
    step = _run(_invocation(HTML_BODY_SOURCE, operation="resolve", payload={"candidate": candidate},
                            actions=[HttpAction(action_id="lx-0", method="GET",
                                                url="https://www.97abc.example/count.php")],
                            observations=[_observation(counter, content_type="text/html; charset=UTF-8")]))
    assert step.response.ok, step.response.error
    assert step.response.result["url"] == "https://cdn.zhihu.example/1/song.mp3?visits=6861378"


LINGERING_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
on(EVENT_NAMES.request, async () => {
  // Shaped like 玉宁熙-Pro: the URL is ready immediately, but the source then
  // starts its own background work -- a telemetry read and a retry timer -- that
  // it never awaits.  The host must answer with the URL, not with a deadline.
  request("https://api.zhihu.example/count.php?id=telemetry", { method: "GET" }, () => {});
  setInterval(() => {}, 30_000);
  return "https://cdn.zhihu.example/1/song.mp3";
});
send(EVENT_NAMES.inited, { status: true, sources: { kg: { name: "酷狗", type: "music", actions: ["musicUrl"], qualitys: ["320k"] } } });
"""


@requires_deno
def test_a_step_arrives_without_waiting_for_the_sources_leftover_work():
    candidate = dict(CANDIDATE, item_id="lx:kg:128014", source_id="kg")
    started = time.monotonic()
    step = _run(_invocation(LINGERING_SOURCE, operation="resolve", payload={"candidate": candidate}))
    elapsed = time.monotonic() - started

    # Without the host exiting once its step is written, the supervisor only
    # reaches the process at its own timeout, so both of these fail together.
    assert elapsed < 5, f"the host held the step for {elapsed:.1f}s of leftover source work"
    assert step.action is not None or step.response.ok, step.response.error


LOAD_TIME_REQUEST_SOURCE = """
const { EVENT_NAMES, request, on, send } = globalThis.lx;
// Shaped like lx-music-source-v6: the source asks a publisher server for its own
// configuration while it loads, and registers its handler only once that answer
// arrives.  Until then it has no handler at all.
request("https://config.zhihu.example/rconfig?e=a04d63ee", { method: "GET" }, (error, response) => {
  if (error) return;
  const config = JSON.parse(response.body);
  on(EVENT_NAMES.request, async () => "https://cdn.zhihu.example/" + config.token + "/song.mp3");
  send(EVENT_NAMES.inited, { sources: { kw: { type: "music", actions: ["musicUrl"] } } });
});
"""


@requires_deno
def test_a_request_made_while_the_source_loads_is_the_very_first_action():
    # The source has no handler to call yet, and the request it made on the way
    # up is still real: dropping it made the whole source look like a plugin
    # with no entrypoint, so the first step carries the configuration read.
    candidate = dict(CANDIDATE, item_id="lx:kw:128014", source_id="kw")
    first = _run(_invocation(LOAD_TIME_REQUEST_SOURCE, operation="resolve",
                             payload={"candidate": candidate}))
    assert first.action is not None, first.response.error
    assert first.action.url == "https://config.zhihu.example/rconfig?e=a04d63ee"

    # Once the answer is on disk the source finishes loading inside this run and
    # answers the resolve it was asked for.
    second = _run(_invocation(LOAD_TIME_REQUEST_SOURCE, operation="resolve", payload={"candidate": candidate},
                              actions=[first.action],
                              observations=[_observation(json.dumps({"token": "abc"}))]))
    assert second.response.ok, second.response.error
    assert second.response.result["url"] == "https://cdn.zhihu.example/abc/song.mp3"
