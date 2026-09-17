function freeze(value) { if(value && typeof value === "object" && !Object.isFrozen(value)) { for(const child of Object.values(value)) freeze(child); Object.freeze(value); } return value; }
const request=freeze(invocation.request), source=invocation.source;
let consoleBudget=8192;
const consoleWrite=(...args)=>{
  if(consoleBudget<=0) return;
  let text="";
  try { text=args.map(value=>typeof value==="string"?value:(()=>{try{return JSON.stringify(value)}catch(_){return String(value)}})()).join(" "); } catch (_) { return; }
  if(!text) return;
  const bytes=new TextEncoder().encode(text.slice(0,1024)+"\n").slice(0,Math.min(consoleBudget,1040));
  consoleBudget-=bytes.length;
  try { Deno.stderr.writeSync(bytes); } catch (_) {}
};
globalThis.console=Object.freeze({log:consoleWrite,info:consoleWrite,debug:consoleWrite,warn:consoleWrite,
  error:consoleWrite,trace:consoleWrite,dir:consoleWrite,table:consoleWrite,group:consoleWrite,
  groupCollapsed:consoleWrite,timeLog:consoleWrite,count:consoleWrite,assert:consoleWrite,
  groupEnd:()=>{},time:()=>{},timeEnd:()=>{},countReset:()=>{},clear:()=>{}});
function stepFrom(value) {
  if (value && typeof value === "object" && Object.keys(value).length === 1 && Object.prototype.hasOwnProperty.call(value, "action")) {
    const a=value.action;
    if (!a || typeof a !== "object" || Array.isArray(a)) throw new Error("invalid action");
    const shape=Object.keys(a).sort().join(",");
    if (shape !== "action_id,method,url" && shape !== "action_id,body,method,url" && shape !== "action_id,headers,method,url" && shape !== "action_id,body,headers,method,url") throw new Error("invalid action");
    if (typeof a.action_id !== "string" || a.action_id.length < 1 || a.action_id.length > 128) throw new Error("invalid action");
    if (a.method !== "GET" && a.method !== "POST") throw new Error("invalid action");
    if (typeof a.url !== "string" || a.url.length < 1 || a.url.length > 4096) throw new Error("invalid action");
    let parsed; try { parsed=new URL(a.url); } catch (_) { throw new Error("invalid action"); }
    if ((parsed.protocol !== "https:" && parsed.protocol !== "http:") || !parsed.hostname || parsed.username || parsed.password || parsed.hash) throw new Error("invalid action");
    if (parsed.port && !/^[0-9]{1,5}$/.test(parsed.port)) throw new Error("invalid action");
    if (a.headers !== undefined) {
      if (!a.headers || typeof a.headers !== "object" || Array.isArray(a.headers) || Object.keys(a.headers).length > 16) throw new Error("invalid action");
      for (const name of Object.keys(a.headers)) {
        const item=a.headers[name];
        if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$/.test(name) || typeof item !== "string" || item.length > 1024 || /[\u0000-\u001f\u007f]/.test(item)) throw new Error("invalid action");
      }
    }
    if (a.body !== undefined && (typeof a.body !== "string" || a.body.length > 87384)) throw new Error("invalid action");
    if (a.method !== "POST" && a.body) throw new Error("invalid action");
    return {response:null, action:a};
  }
  return {response:{protocol:"musicdl.plugin/v1",request_id:request.request_id,operation:request.operation,ok:true,result:value},action:null};
}
Object.defineProperty(globalThis,"Worker",{value:undefined,writable:false,configurable:false});
Object.defineProperty(globalThis,"process",{value:undefined,writable:false,configurable:false});
try {
  const fn=new Function("musicdlRequest", `"use strict"; const Worker=undefined; const process=undefined; ${source}\n; return typeof handle === "function" ? handle(musicdlRequest) : undefined;`);
  const result=await fn(request);
  if(result===undefined) throw new Error("missing handle");
  await Deno.stdout.write(new TextEncoder().encode(JSON.stringify(stepFrom(result))));
} catch (_) {
  await Deno.stdout.write(new TextEncoder().encode(JSON.stringify({response:{protocol:"musicdl.plugin/v1",request_id:request?.request_id??"00000000-0000-0000-0000-000000000000",operation:request?.operation??"health",ok:false,error:{code:"plugin_error",message:"plugin execution failed",retryable:false,details:{}}},action:null})));
}
// The step above is written and awaited, so this invocation is over.  A source
// that keeps its own async work alive after answering -- 玉宁熙 starts a
// telemetry read and a retry timer after returning its first URL -- would
// otherwise hold this process open until that leftover chain settles, and the
// supervisor only reads the step once the process exits.  One step per process
// means the answer decides when the process ends.
Deno.exit(0);
