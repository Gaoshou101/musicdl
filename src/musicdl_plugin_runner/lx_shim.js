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
 * clock or a random number between two steps would ask for something else this
 * time.  Answering that with the recorded response would hand one call another
 * call's data, so a replayed call that no longer matches its recorded URL and
 * method is refused instead of answered.
 *
 * An lx source takes its runtime from `globalThis.lx` and reports through
 * `on(EVENT_NAMES.request, ...)`.  The host still looks up a global `handle`,
 * which no lx source declares, so installing one here leaves `deno_host.js`
 * itself unchanged and a source that declares its own `handle` still shadows
 * this adapter.
 */

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

function lxRequest(url, options, callback) {
  const index = lxCalls;
  lxCalls += 1;
  const settings = options && typeof options === "object" ? options : {};
  const method = String(settings.method || "GET").toUpperCase();
  const target = typeof url === "string" ? url : String(url);
  const observation = invocation.observations[index];
  if (observation !== undefined && observation !== null) {
    const recorded = invocation.actions[index];
    if (!recorded || recorded.url !== target || String(recorded.method || "GET").toUpperCase() !== method) {
      throw new Error("lx source does not replay deterministically");
    }
    if (typeof callback === "function") {
      callback(null, {
        statusCode: observation.status_code,
        headers: observation.headers || {},
        body: lxFromBase64(observation.body),
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
  if (Object.prototype.hasOwnProperty.call(LX_MEDIA_TYPES, suffix)) return suffix;
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
  // Sources probe this before using it (a source that needs it checks
  // `lx.utils?.buffer?.bufToString` first), so an empty object is enough.
  utils: {},
};

globalThis.handle = async function lxHandle(request) {
  // A plain JavaScript plugin registers no lx handler; leaving the host without
  // a `handle` keeps its own "missing handle" failure rather than inventing one.
  if (typeof lxHandler !== "function") return undefined;
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
