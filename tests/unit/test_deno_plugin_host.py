import asyncio,hashlib,json,os,shutil,pytest
from pathlib import Path
from musicdl_plugin_runner.supervisor import build_host_command
from musicdl.contracts.plugin import PluginInvocation,PluginManifest,PluginRequest
import hashlib
from uuid import uuid4
def test_deno_source_has_no_permission_escape():
 text=(Path(__file__).parents[2]/"src/musicdl_plugin_runner/deno_host.js").read_text(encoding="utf-8")
 assert "Object.defineProperty(globalThis,\"Worker\"" in text and "--allow" not in text
def test_deno_command_exact():
 source="function handle(r){return {hits:[]}}"
 inv=PluginInvocation(manifest=PluginManifest(plugin_id="p",version="1",language="javascript",operations=("search",),sha256=hashlib.sha256(source.encode()).hexdigest()),source=source,request=PluginRequest(protocol="musicdl.plugin/v1",request_id=uuid4(),operation="search"))
 c=build_host_command(inv)
 assert c.argv[1:]==["run","--quiet","--no-config","--no-lock","--no-npm","--cached-only","--v8-flags=--max-old-space-size=128","-"] and c.env=={"DENO_NO_PROMPT":"1"}
def _invocation(source):
 return PluginInvocation(manifest=PluginManifest(plugin_id="p",version="1",language="javascript",operations=("search",),sha256=hashlib.sha256(source.encode()).hexdigest()),source=source,request=PluginRequest(protocol="musicdl.plugin/v1",request_id=uuid4(),operation="search"))

def test_deno_payload_is_safely_embedded():
 source="function handle(r){return {text: `quote' \" ${r.payload.x}\\nline`}}"
 c=build_host_command(_invocation(source))
 payload=c.stdin_payload.decode()
 assert "JSON.parse(" in payload and "--allow" not in payload
 encoded=payload.split("JSON.parse(",1)[1].split(");",1)[0]
 assert json.loads(json.loads(encoded))['source'] == source

def test_deno_valid_runtime_when_available():
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 async def run(): return await __import__('musicdl_plugin_runner.supervisor',fromlist=['Supervisor']).Supervisor(build_host_command).execute(_invocation("function handle(r){return {hits: []}}"))
 step=asyncio.run(run())
 assert step.response.ok and step.response.result == {"hits": []}

def test_deno_action_runtime_when_available():
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 from musicdl_plugin_runner.supervisor import Supervisor
 step=asyncio.run(Supervisor(build_host_command).execute(_invocation("function handle(r){return {action:{action_id:'a1',method:'GET',url:'https://example.com/x'}}}")))
 assert step.action and step.action.action_id == "a1"

@pytest.mark.parametrize("value", [
 "{action:{action_id:'a1',method:'POST',url:'https://example.com'}}",
 "{action:{action_id:'a1',method:'GET',url:'http://example.com'}}",
 "{action:{action_id:'a1',method:'GET',url:'https://example.com:444'}}",
 "{action:{action_id:'a1',method:'GET',url:'https://u:p@example.com'}}",
 "{action:{action_id:'a1',method:'GET',url:'https://example.com',extra:1}}",
])
def test_deno_malformed_action_is_plugin_error(value):
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 from musicdl_plugin_runner.supervisor import Supervisor
 step=asyncio.run(Supervisor(build_host_command).execute(_invocation(f"function handle(r){{return {value}}}")))
 assert step.response.error.code == "plugin_error"

def test_deno_infinite_loop_times_out():
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 inv=_invocation("function handle(r){ while(true){} }")
 inv=inv.model_copy(update={"request":inv.request.model_copy(update={"timeout_ms":100})})
 from musicdl_plugin_runner.supervisor import Supervisor
 step=asyncio.run(Supervisor(build_host_command).execute(inv))
 assert step.response.error.code == "timeout"

@pytest.mark.parametrize("source", [
 "function handle(r){ process.exit() }", "function handle(r){ require('os') }",
 "async function handle(r){ await fetch('https://example.invalid') }",
 "function handle(r){ Deno.env.get('HOME') }", "async function handle(r){ await Deno.readTextFile('/etc/passwd') }",
 "async function handle(r){ await new Deno.Command('id').output() }", "function handle(r){ Deno.dlopen('x', {}) }",
 "function handle(r){ new Worker('data:text/javascript,') }",
])
def test_deno_hostile_runtime_is_sanitized(source):
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 from musicdl_plugin_runner.supervisor import Supervisor
 step=asyncio.run(Supervisor(build_host_command).execute(_invocation(source)))
 assert step.response.error.code == "plugin_error"
 assert "example.invalid" not in step.response.error.message and "passwd" not in step.response.error.message
