import shutil,pytest
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
 assert c.argv==["deno","run","--quiet","--no-config","--no-lock","--no-npm","--cached-only","--v8-flags=--max-old-space-size=128","-"] and c.env=={"DENO_NO_PROMPT":"1"}
def test_deno_command_contract():
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 pytest.skip("runtime exercised in integration acceptance suite")
