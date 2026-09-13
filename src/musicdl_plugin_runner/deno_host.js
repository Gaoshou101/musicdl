function freeze(value) { if(value && typeof value === "object" && !Object.isFrozen(value)) { for(const child of Object.values(value)) freeze(child); Object.freeze(value); } return value; }
const request=freeze(invocation.request), source=invocation.source;
Object.defineProperty(globalThis,"Worker",{value:undefined,writable:false,configurable:false});
try {
  const fn=new Function("request", `"use strict"; const Worker=undefined; ${source}\n; return typeof handle === "function" ? handle(request) : undefined;`);
  const result=await fn(request);
  if(result===undefined) throw new Error("missing handle");
  await Deno.stdout.write(new TextEncoder().encode(JSON.stringify({response:{protocol:"musicdl.plugin/v1",request_id:request.request_id,operation:request.operation,ok:true,result},action:null})));
} catch (_) {
  await Deno.stdout.write(new TextEncoder().encode(JSON.stringify({response:{protocol:"musicdl.plugin/v1",request_id:request?.request_id??"00000000-0000-0000-0000-000000000000",operation:request?.operation??"health",ok:false,error:{code:"plugin_error",message:"plugin execution failed",retryable:false,details:{}}},action:null})));
}
