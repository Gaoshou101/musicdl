import hashlib,json,os,subprocess,sys,pytest
from pathlib import Path
from uuid import uuid4
from musicdl.contracts.plugin import PluginInvocation,PluginManifest,PluginRequest
from musicdl_plugin_runner.python_host import build_command
def invoke(source):
 m=PluginManifest(plugin_id="p",version="1",language="python",operations=("search",),sha256=hashlib.sha256(source.encode()).hexdigest()); return PluginInvocation(manifest=m,source=source,request=PluginRequest(protocol="musicdl.plugin/v1",request_id=uuid4(),operation="search"))
def run(source):
 env = dict(os.environ)
 env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
 return subprocess.run([sys.executable,"-m","musicdl_plugin_runner.python_host"],input=invoke(source).model_dump_json(),text=True,capture_output=True,env=env)
def test_valid():
 p=run("def handle(request): return {'hits': []}"); response=json.loads(p.stdout)["response"]
 assert response["ok"] if os.name == "posix" else response["error"]["code"]=="sandbox_unavailable"
def test_hostile_open_is_sanitized():
 p=run("def handle(request): open('/etc/passwd')"); assert json.loads(p.stdout)["response"]["error"]["code"] in {"plugin_error","resource_limit","sandbox_unavailable"}
def test_import_is_unavailable():
 p=run("def handle(request): __import__('os')"); code=json.loads(p.stdout)["response"]["error"]["code"]
 assert code == ("plugin_error" if os.name == "posix" else "sandbox_unavailable")
def test_command_is_isolated():
 c=build_command(invoke("def handle(request): return {}")); assert c.env=={} and c.argv[1:3]==["-I","-m"]
