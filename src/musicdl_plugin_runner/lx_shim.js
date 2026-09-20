/* lx-music custom-source adapter.
 *
 * The supervisor runs one plugin step per process and replays every observation
 * it has already collected, so this adapter is a deterministic replay machine
 * rather than a live runtime: the Nth call to `request()` in this process is
 * answered from `invocation.observations[N]`, and the first call with no
 * recorded answer becomes the action this step returns.  The main process
 * performs that action and re-runs the whole source with one more observation,
 * which is how a source written for a long-lived browser host gets to finish.
 *
 * The source is re-read from disk before every step, so a source that reads a
 * clock or a random number between two steps asks for a slightly different URL
 * this time.  The ordinal still decides: the recorded answer for call N was
 * fetched under this source's own policy, for call N, one step ago.  What
 * would hand one call another call's data is a *different* call moving into
 * this ordinal, and the first few of those are noted on the host's bounded
 * stderr rather than refused -- 玉宁熙-Pro rebuilds a random `user`/`loginUid`
 * pair on every run, and refusing the moved URL made a working source
 * unresolvable.
 *
 * An lx source takes its runtime from `globalThis.lx` and reports through
 * `on(EVENT_NAMES.request, ...)`.  The host still looks up a global `handle`,
 * which no lx source declares, so installing one here leaves `deno_host.js`
 * itself unchanged and a source that declares its own `handle` still shadows
 * this adapter.
 */

// The publisher's runtime also carries a byte/string toolkit, and the sources
// that need it call the members directly instead of feature-detecting them.
// Measured on 2026-09-17 against the twelve supplied sources: 星海音乐源 probes
// `lx.utils?.buffer?.bufToString`, while the obfuscated 野花音源, 野草音源 and
// lx-music-source-v6 die inside the source with "Cannot read properties of
// undefined (reading 'bufToString'/'md5')", which is what an empty `utils`
// produces.  These are the members the sources actually reach for; a member
// nothing has asked for is still absent, so an unmet call stays a loud failure
// inside the source rather than a silently wrong answer.
import { createHash } from "node:crypto";

const LX_EVENTS = Object.freeze({ request: "request", inited: "inited", updateAlert: "updateAlert" });
const LX_ITEM_PREFIX = "lx:";
const LX_MAX_CANDIDATES = 100;
const LX_DEFAULT_QUALITY = "320k";
const LX_QUALITIES = new Set(["128k", "192k", "320k", "flac", "flac24bit"]);
const LX_MEDIA_TYPES = Object.freeze({
  mp3: "audio/mpeg",
  flac: "audio/flac",
  m4a: "audio/mp4",
  ogg: "audio/ogg",
});
// A link's suffix is what the CDN chose to call the file, and that is all it
// is: measured 2026-09-17, kuwo's car CDN answers `car-bj.kuwo.cn/.../1904613985.aac`
// with an ISO base media file -- ftyp/mp42 with the brands `M4A `, `mp42`,
// `isom` -- and measured 2026-09-20 the same shape of link answered with plain
// ADTS frames.  Declared as a guessed mp3 instead, four sources resolved a real
// song and the transport refused the bytes as the wrong container, so `.aac`
// stays declared as an m4a here as the better of the two guesses.  The
// declaration is a hint either way: the transport classifies the bytes it reads
// and publishes the file under the container they announce.  Every other suffix
// falls through to the quality guess.
const LX_SUFFIX_EXTENSIONS = Object.freeze({ aac: "m4a" });
// The main process writes the request line and the framing headers itself, so
// a source may not supply one of them.
const LX_TRANSPORT_HEADERS = new Set([
  "connection", "content-length", "expect", "host", "keep-alive",
  "proxy-authorization", "proxy-connection", "te", "trailer",
  "transfer-encoding", "upgrade",
]);
const LX_HEADER_NAME = /^[!#$%&'*+.^_\x60|~0-9A-Za-z-]{1,64}$/;
const LX_JSON_BODY = /^[\s]*[\[{"]/;

let lxHandler = null;
let lxSources = null;
let lxCalls = 0;
let lxPending = null;
let lxOutcome = null;
let lxWake = null;
const lxWaiting = new Promise((resolve) => { lxWake = resolve; });
const lxNoop = () => {};

function lxFromBase64(value) {
  if (typeof value !== "string" || value.length === 0) return "";
  let binary;
  try {
    binary = atob(value);
  } catch (_) {
    return "";
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new TextDecoder("utf-8").decode(bytes);
}

function lxToBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index]);
  return btoa(binary);
}

// The publisher's host hands back a body that is already decoded when the
// upstream answered with JSON, and the supplied sources are written against
// exactly that.  Every readable one guards its own parse -- 星海音乐源 and
// 聚合音源 特供版 run `typeof body === "string" ? JSON.parse(body) : body`, K×H
// writes `let body = resp.body; if (typeof body === "string") { body =
// JSON.parse(...) }`, and HYWmusic does the same three times -- while 玉宁熙-Pro
// reads `response.body.code` and `response.body.data.url` with no guard at all
// and reports "酷我音乐解析失败" against a string.
//
// Handing over a bare parsed object would satisfy the first group and break any
// source that calls `JSON.parse(response.body)` unguarded, so the adapter hands
// over the parsed value dressed as the text it came from: property reads answer
// from the JSON, `JSON.parse(body)`, `String(body)` and every string method
// still see the exact bytes upstream sent, and a `typeof` test sees the object
// the publisher's host would have given.
function lxResponseBody(text) {
  if (typeof text !== "string" || text.length === 0) return "";
  const head = text.trimStart().slice(0, 1);
  if (head !== "{" && head !== "[") return text;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (_) {
    return text;
  }
  if (!parsed || typeof parsed !== "object") return text;
  if (Array.isArray(parsed)) {
    // An array keeps its identity -- `Array.isArray` and `body[0]` are what a
    // source checks -- and still stringifies to the raw body.
    Object.defineProperty(parsed, "toString", { value: () => text, configurable: true });
    Object.defineProperty(parsed, Symbol.toPrimitive, { value: () => text, configurable: true });
    return parsed;
  }
  return new Proxy(parsed, {
    get(target, property, receiver) {
      if (property === Symbol.toPrimitive || property === "toString" || property === "valueOf") {
        return () => text;
      }
      if (Reflect.has(target, property)) return Reflect.get(target, property, receiver);
      // A source that treats the body as text still gets the text's own members.
      const member = text[property];
      return typeof member === "function" ? member.bind(text) : member;
    },
  });
}

const LX_TEXT_ENCODER = new TextEncoder();
const LX_TEXT_DECODER = new TextDecoder("utf-8");

function lxBytes(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  if (Array.isArray(value)) {
    const bytes = new Uint8Array(value.length);
    for (let index = 0; index < value.length; index += 1) bytes[index] = Number(value[index]) & 0xff;
    return bytes;
  }
  return LX_TEXT_ENCODER.encode(typeof value === "string" ? value : "");
}

function lxEncoding(value) {
  // The publisher spells the same encoding as "utf-8", "utf8", or "UTF8"
  // depending on the source, and defaults to hex when it says nothing.
  const name = String(value === undefined || value === null ? "hex" : value).trim().toLowerCase();
  if (name === "utf8" || name === "utf-8") return "utf8";
  if (name === "base64") return "base64";
  return "hex";
}

function lxBufToString(value, encoding) {
  const bytes = lxBytes(value);
  const kind = lxEncoding(encoding);
  if (kind === "utf8") return LX_TEXT_DECODER.decode(bytes);
  let text = "";
  if (kind === "base64") {
    for (let index = 0; index < bytes.length; index += 1) text += String.fromCharCode(bytes[index]);
    return btoa(text);
  }
  for (let index = 0; index < bytes.length; index += 1) text += bytes[index].toString(16).padStart(2, "0");
  return text;
}

function lxStringToBuf(value, encoding) {
  const text = typeof value === "string" ? value : String(value);
  const kind = lxEncoding(encoding);
  if (kind === "utf8") return LX_TEXT_ENCODER.encode(text).buffer;
  if (kind === "base64") {
    let binary;
    try {
      binary = atob(text);
    } catch (_) {
      binary = "";
    }
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes.buffer;
  }
  const bytes = new Uint8Array(Math.floor(text.length / 2));
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(text.slice(index * 2, index * 2 + 2), 16) & 0xff;
  }
  return bytes.buffer;
}

function lxMd5(value) {
  const bytes = typeof value === "string" ? LX_TEXT_ENCODER.encode(value) : lxBytes(value);
  return createHash("md5").update(bytes).digest("hex");
}

// A source that mixes a random nonce into a URL is replayed by re-running the
// whole script once per step, so the bytes must repeat across runs of one step
// and still advance between one call and the next: a source that asks twice
// must not receive one value.  Real randomness cannot do that here, and a
// toolkit that hands out a fresh nonce per run would make every signed request
// move, so the adapter carries a fixed-seed stream instead.
let lxRandomState = 0x9e3779b9;

function lxRandomUnit() {
  lxRandomState = (lxRandomState + 0x6d2b79f5) >>> 0;
  let value = lxRandomState;
  value = Math.imul(value ^ (value >>> 15), value | 1) >>> 0;
  value = (value ^ (value + Math.imul(value ^ (value >>> 7), value | 61))) >>> 0;
  return (value ^ (value >>> 14)) >>> 0;
}

function lxRandomBytes(size) {
  const count = Number.isFinite(size) && size > 0 ? Math.min(Math.floor(size), 4096) : 0;
  const bytes = new Uint8Array(count);
  for (let index = 0; index < count; index += 1) {
    bytes[index] = lxRandomUnit() & 0xff;
  }
  return bytes.buffer;
}

// `Math.random` is where the supplied sources actually reach, and it carries the
// same requirement: 玉宁熙-Pro draws its kuwo `user`/`loginUid` pair from it, and
// nmobi.kuwo.cn echoes that user back inside the JSON it answers with, so a
// fresh draw on the re-run made the source reject its own, already fetched answer
// as "酷我音乐解析失败".  The stream is seeded, so it repeats per process and
// still advances between two draws inside one run.
Math.random = function lxRandom() {
  return lxRandomUnit() / 4294967296;
};

const LX_UTILS = Object.freeze({
  buffer: Object.freeze({
    bufToString: lxBufToString,
    stringToBuf: lxStringToBuf,
    from: lxStringToBuf,
  }),
  crypto: Object.freeze({ md5: lxMd5, randomBytes: lxRandomBytes }),
});

function lxOn(name, handler) {
  if (name === LX_EVENTS.request && typeof handler === "function") lxHandler = handler;
}

function lxSend(name, data) {
  if (name !== LX_EVENTS.inited) return;
  if (!data || typeof data !== "object") return;
  if (data.sources && typeof data.sources === "object") lxSources = data.sources;
}

function lxHeaders(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const selected = {};
  const seen = new Set();
  for (const name of Object.keys(value)) {
    const item = value[name];
    if (typeof item !== "string" || !LX_HEADER_NAME.test(name)) continue;
    const lowered = name.toLowerCase();
    if (LX_TRANSPORT_HEADERS.has(lowered) || seen.has(lowered)) continue;
    // The main process can only reproduce a value it can encode as bytes.
    if (item.length > 1024 || /[^\x20-\x7e]/.test(item)) continue;
    if (seen.size >= 16) break;
    seen.add(lowered);
    selected[name] = item;
  }
  return seen.size ? selected : null;
}

function lxBody(value, method) {
  if (method !== "POST" || value === undefined || value === null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (typeof text !== "string" || text.length === 0) return "";
  return lxToBase64(text);
}

function lxAction(index, method, url, settings) {
  const action = { action_id: "lx-" + index, method: method === "POST" ? "POST" : "GET", url };
  const headers = lxHeaders(settings.headers);
  if (headers !== null) action.headers = headers;
  const body = lxBody(settings.body, action.method);
  if (body) action.body = body;
  return action;
}

// A moved call is worth seeing once, but the note is a diagnostic and not a
// refusal: the answer below is the one recorded for this ordinal, which is what
// a live host would have delivered to this call.
let lxMovedCalls = 0;

function lxNoteMovedCall(index, method, target) {
  if (lxMovedCalls >= 4) return;
  const recorded = invocation.actions[index];
  if (!recorded || recorded.url === undefined || recorded.url === null) return;
  const wanted = String(recorded.url);
  const wantedMethod = String(recorded.method || "GET").toUpperCase();
  if (wanted === target && wantedMethod === method) return;
  lxMovedCalls += 1;
  console.error("lx replay: call " + index + " moved from " + wantedMethod + " " + wanted
                + " to " + method + " " + target);
}

function lxRequest(url, options, callback) {
  const index = lxCalls;
  lxCalls += 1;
  const settings = options && typeof options === "object" ? options : {};
  const method = String(settings.method || "GET").toUpperCase();
  const target = typeof url === "string" ? url : String(url);
  const observation = invocation.observations[index];
  if (observation !== undefined && observation !== null) {
    lxNoteMovedCall(index, method, target);
    if (typeof callback === "function") {
      callback(null, {
        statusCode: observation.status_code,
        headers: observation.headers || {},
        body: lxResponseBody(lxFromBase64(observation.body)),
      });
    }
    return lxNoop;
  }
  // A source may fan out several requests at once -- the analysed aggregate
  // source races three handlers -- and a step carries only one action.  The
  // first unanswered call becomes that action; the rest stay unanswered here
  // and are answered by the next re-run, because the source re-issues them in
  // the same order and one more observation is on disk by then.
  if (lxPending === null) {
    lxPending = lxAction(index, method, target, settings);
    lxWake();
  }
  return lxNoop;
}

function lxText(value, maxLength) {
  if (value === undefined || value === null) return "";
  const text = String(value).replace(/\s+/g, " ").trim();
  return text.length > maxLength ? text.slice(0, maxLength).trim() : text;
}

function lxSongId(item) {
  for (const key of ["id", "songmid", "songId", "hash", "rid", "mid", "strMediaMid", "mediaId"]) {
    const value = item[key];
    if (typeof value === "string" && value.length) return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

function lxCandidate(source, item) {
  if (!item || typeof item !== "object") return null;
  const songId = lxSongId(item);
  const title = lxText(item.name, 500);
  const artist = lxText(item.singer, 500);
  if (!songId || !title || !artist) return null;
  const album = lxText(item.albumName, 500);
  const duration = Number(item.duration);
  return {
    source_id: invocation.manifest.plugin_id,
    source_version: invocation.manifest.version,
    item_id: LX_ITEM_PREFIX + source + ":" + songId,
    title,
    artist,
    album: album || null,
    duration: Number.isFinite(duration) && duration > 0 ? Math.min(Math.floor(duration), 86400) : null,
    bitrate: null,
    format: null,
    size: null,
  };
}

function lxSearchSource(requested) {
  const sources = lxSources && typeof lxSources === "object" ? lxSources : null;
  if (sources === null) return null;
  if (typeof requested === "string" && Object.prototype.hasOwnProperty.call(sources, requested)) return requested;
  for (const key of Object.keys(sources)) {
    const declared = sources[key] && sources[key].actions;
    if (Array.isArray(declared) && (declared.includes("musicSearch") || declared.includes("search"))) return key;
  }
  return null;
}

async function lxSearch(payload) {
  const source = lxSearchSource(payload.source);
  if (source === null) throw new Error("lx source declares no search action");
  const keyword = typeof payload.query === "string" ? payload.query : "";
  const page = Number.isInteger(payload.page) && payload.page > 0 ? payload.page : 1;
  const pagesize = Number.isInteger(payload.pagesize) && payload.pagesize > 0 ? Math.min(payload.pagesize, 100) : 30;
  const answer = await lxHandler({ source, action: "musicSearch", info: { keyword, page, pagesize } });
  const list = answer && Array.isArray(answer.list) ? answer.list : [];
  const candidates = [];
  for (const item of list) {
    if (candidates.length >= LX_MAX_CANDIDATES) break;
    const candidate = lxCandidate(source, item);
    if (candidate !== null) candidates.push(candidate);
  }
  return candidates;
}

function lxDecodeItemId(value) {
  if (typeof value !== "string" || value.slice(0, LX_ITEM_PREFIX.length) !== LX_ITEM_PREFIX) return null;
  const rest = value.slice(LX_ITEM_PREFIX.length);
  const separator = rest.indexOf(":");
  if (separator <= 0 || separator === rest.length - 1) return null;
  return { source: rest.slice(0, separator), songId: rest.slice(separator + 1) };
}

function lxMusicInfo(songId, candidate) {
  const duration = Number(candidate.duration);
  return {
    id: songId,
    songmid: songId,
    songId: songId,
    hash: songId,
    mid: songId,
    name: typeof candidate.title === "string" ? candidate.title : "",
    singer: typeof candidate.artist === "string" ? candidate.artist : "",
    albumName: typeof candidate.album === "string" ? candidate.album : "",
    duration: Number.isFinite(duration) && duration > 0 ? duration : 0,
  };
}

function lxAnswerUrl(answer) {
  if (typeof answer === "string") return answer.trim();
  if (answer && typeof answer === "object") {
    for (const key of ["url", "musicUrl"]) {
      const value = answer[key];
      if (typeof value === "string" && value.length) return value.trim();
    }
  }
  return "";
}

function lxExtension(url, quality) {
  let pathname;
  try {
    pathname = new URL(url).pathname;
  } catch (_) {
    throw new Error("lx source returned an unusable media URL");
  }
  const dot = pathname.lastIndexOf(".");
  const suffix = dot === -1 ? "" : pathname.slice(dot + 1).toLowerCase();
  const named = Object.prototype.hasOwnProperty.call(LX_SUFFIX_EXTENSIONS, suffix)
    ? LX_SUFFIX_EXTENSIONS[suffix] : suffix;
  if (Object.prototype.hasOwnProperty.call(LX_MEDIA_TYPES, named)) return named;
  // Most endpoints these sources fall back to hand back a direct link whose
  // path carries no audio suffix (`/wy/wy.php?type=mp3&id=...`), so the quality
  // the source was asked for is the only container it ever states.  The
  // transport still checks the response content type against it, so a wrong
  // container fails loudly rather than being written to disk.
  return quality === "flac" || quality === "flac24bit" ? "flac" : "mp3";
}

async function lxResolve(payload) {
  const candidate = payload.candidate;
  if (!candidate || typeof candidate !== "object") throw new Error("lx resolve requires a candidate");
  const decoded = lxDecodeItemId(candidate.item_id);
  if (decoded === null) throw new Error("candidate was not produced by this lx source");
  const quality = typeof payload.quality === "string" && LX_QUALITIES.has(payload.quality)
    ? payload.quality : LX_DEFAULT_QUALITY;
  const answer = await lxHandler({
    source: decoded.source,
    action: "musicUrl",
    info: { type: quality, musicInfo: lxMusicInfo(decoded.songId, candidate) },
  });
  const url = lxAnswerUrl(answer);
  if (!url) throw new Error("lx source returned no media URL");
  const extension = lxExtension(url, quality);
  return {
    candidate_id: String(candidate.item_id),
    url,
    extension,
    media_type: LX_MEDIA_TYPES[extension],
    declared_size: null,
  };
}

function lxDispatch(request) {
  const operation = request && request.operation;
  const payload = request && request.payload && typeof request.payload === "object" ? request.payload : {};
  if (operation === "search") return lxSearch(payload);
  if (operation === "resolve") return lxResolve(payload);
  throw new Error("lx source does not implement " + String(operation));
}

globalThis.lx = {
  EVENT_NAMES: LX_EVENTS,
  request: lxRequest,
  on: lxOn,
  send: lxSend,
  // Every analysed source concatenates `env` into a request header rather than
  // calling into it, so a string is what the observed contract asks for.
  env: "musicdl",
  version: "musicdl.plugin/v1",
  currentScriptInfo: { name: invocation.manifest.plugin_id, version: invocation.manifest.version },
  utils: LX_UTILS,
};

globalThis.handle = async function lxHandle(request) {
  // A plain JavaScript plugin registers no lx handler; leaving the host without
  // a `handle` keeps its own "missing handle" failure rather than inventing one.
  if (typeof lxHandler !== "function") {
    // A source may fetch its own configuration while it loads and register
    // nothing until that answer arrives: lx-music-source-v6 asks a publisher
    // server for `rconfig` before it has a handler at all.  The request it made
    // is still this step's action, so report it rather than calling the source
    // entrypointless; the answer is on disk before the next run.
    for (let turn = 0; turn < 200 && lxHandler === null && lxPending === null; turn += 1) {
      if (turn % 25 === 24) await new Promise((resolve) => setTimeout(resolve, 0));
      else await Promise.resolve();
    }
    if (typeof lxHandler !== "function") {
      return lxPending === null ? undefined : { action: lxPending };
    }
  }
  const outcome = Promise.resolve().then(() => lxDispatch(request)).then(
    (value) => ({ kind: "result", value }),
    (error) => ({ kind: "error", error }),
  );
  outcome.then((value) => { lxOutcome = value; });
  let settled = await Promise.race([outcome, lxWaiting.then(() => ({ kind: "waiting" }))]);
  if (settled.kind === "waiting") {
    // A source that issued a request without waiting for it still gets to
    // answer: two turns let its own promise chain settle before the request
    // it never awaited is reported as the step's action.
    await Promise.resolve();
    await Promise.resolve();
    if (lxOutcome !== null) settled = lxOutcome;
  }
  if (settled.kind === "result") return settled.value;
  if (settled.kind === "error") throw settled.error;
  return { action: lxPending };
};
