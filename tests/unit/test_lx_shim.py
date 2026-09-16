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


def _observation(body, *, action_id="lx-0", status_code=200):
    encoded = base64.b64encode(body.encode() if isinstance(body, str) else body).decode("ascii")
    return HttpObservation(action_id=action_id, status_code=status_code,
                           headers={"content-type": "application/json"}, body=encoded)


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
def test_a_source_that_asks_something_else_on_replay_is_refused():
    # The source is re-read before every step.  Answering a call with another
    # call's recorded response is the one failure mode that would look like
    # success, so it is refused instead.
    elsewhere = [HttpAction(action_id="lx-0", method="GET",
                            url="https://api.zhihu.example/search?keywords=other")]
    step = _run(_invocation(HANDLER_SOURCE, payload={"query": "hello"}, actions=elsewhere,
                            observations=[_observation(json.dumps({"list": []}))]))
    assert step.response.ok is False and step.response.error.code == "plugin_error"


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
