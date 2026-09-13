import hashlib,json,os,subprocess,sys,pytest
from pathlib import Path
from uuid import uuid4
from musicdl.contracts.plugin import PluginInvocation,PluginManifest,PluginRequest
from musicdl_plugin_runner.python_host import build_command
import musicdl_plugin_runner.python_host as host
import musicdl_plugin_runner.seccomp as seccomp
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
    c=build_command(invoke("def handle(request): return {}")); assert c.env=={} and c.argv[1:3]==["-I","-c"]

@pytest.mark.parametrize("snippet", [
    "open('/proc/1/environ').read()",
    "__import__('os')",
    "__import__('socket').socket()",
    "__import__('os').fork()",
    "__import__('subprocess').run([])",
    "__import__('ctypes').CDLL(None)",
    "__import__('signal').raise_signal(2)",
])
def test_hostile_operations_are_sanitized(snippet):
    if os.name != "posix": pytest.skip("POSIX restricted runtime unavailable")
    p=run(f"def handle(request):\n    {snippet}")
    assert p.returncode == 0
    response=json.loads(p.stdout)["response"]
    assert response["ok"] is False and response["error"]["code"] in {"plugin_error","sandbox_unavailable","resource_limit"}

@pytest.mark.skipif(os.name != "posix", reason="POSIX CPU rlimit runtime")
def test_infinite_cpu_is_timed_out():
    p=run("def handle(request):\n    while True: pass")
    assert p.returncode != 0 or (p.stdout and json.loads(p.stdout)["response"]["error"]["code"] in {"resource_limit","plugin_failed"})

@pytest.mark.skipif(os.name != "posix", reason="POSIX restricted runtime")
def test_valid_result_is_deterministic():
    source="def handle(request): return {'hits': [{'id': 'fixed'}]}"
    first, second = run(source), run(source)
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout) == json.loads(second.stdout)

def test_mocked_host_applies_exact_limits_and_seccomp_before_source(monkeypatch):
    calls=[]
    class R:
        RLIMIT_CPU=1; RLIMIT_AS=2; RLIMIT_FSIZE=3; RLIMIT_NOFILE=4; RLIMIT_CORE=5
        def setrlimit(self, kind, value): calls.append(("rlimit",kind,value))
    env={"SECRET":"present"}
    monkeypatch.setattr(host, "resource", R())
    monkeypatch.setattr(host.os, "environ", env)
    monkeypatch.setattr(host.seccomp, "install", lambda: calls.append(("seccomp",)) or True)
    inv=invoke("def handle(request): return {'ok': True}")
    step=host._run(inv)
    assert step.response.ok
    assert calls == [("rlimit",1,(5,5)), ("rlimit",2,(256*1024*1024,256*1024*1024)), ("rlimit",3,(1024*1024,1024*1024)), ("rlimit",4,(32,32)), ("rlimit",5,(0,0)), ("seccomp",)]
    assert env == {}

def test_mocked_host_fails_closed_before_source_sentinel(monkeypatch):
    class R:
        RLIMIT_CPU=1; RLIMIT_AS=2; RLIMIT_FSIZE=3; RLIMIT_NOFILE=4; RLIMIT_CORE=5
        def setrlimit(self, *_): pass
    monkeypatch.setattr(host, "resource", R())
    monkeypatch.setattr(host.seccomp, "install", lambda: (_ for _ in ()).throw(RuntimeError("no policy")))
    inv=invoke("raise RuntimeError('source sentinel')")
    step=host._run(inv)
    assert step.response.error.code == "sandbox_unavailable"

def test_seccomp_fake_library_installs_complete_deny_policy(monkeypatch):
    if os.name != "posix": pytest.skip("libseccomp is POSIX-only")
    calls=[]
    class Fn:
        def __init__(self, fn): self.fn=fn
        def __call__(self,*args): return self.fn(*args)
    class Lib:
        def __init__(self):
            self.seccomp_init=Fn(lambda action: calls.append(("init",action)) or 7)
            self.seccomp_syscall_resolve_name=Fn(lambda name: 42)
            self.seccomp_rule_add=Fn(lambda ctx, action, num, count: calls.append(("rule",action,num,count)) or 0)
            self.seccomp_load=Fn(lambda ctx: calls.append(("load",ctx)) or 0)
            self.seccomp_release=Fn(lambda ctx: calls.append(("release",ctx)))
    lib=Lib(); monkeypatch.setattr(seccomp.ctypes, "CDLL", lambda name: lib)
    assert seccomp.install() is True
    assert calls[0] == ("init",0x7FFF0000)
    assert len([c for c in calls if c[0] == "rule"]) == len(seccomp._DENIED)
    assert all(c[1] == (0x00050000 | 1) for c in calls if c[0] == "rule")
    assert calls[-1] == ("release",7)

@pytest.mark.parametrize("failure", ["init", "rule", "load"])
def test_seccomp_fake_library_errors_release_context(monkeypatch, failure):
    if os.name != "posix": pytest.skip("libseccomp is POSIX-only")
    class Fn:
        def __init__(self, fn): self.fn=fn
        def __call__(self,*args): return self.fn(*args)
    class Lib:
        def __init__(self):
            self.seccomp_init=Fn(lambda action: 0 if failure == "init" else 7)
            self.seccomp_syscall_resolve_name=Fn(lambda name: 42)
            self.seccomp_rule_add=Fn(lambda *args: -1 if failure == "rule" else 0)
            self.seccomp_load=Fn(lambda ctx: -1 if failure == "load" else 0)
            self.released=False
            self.seccomp_release=Fn(lambda ctx: setattr(self,"released",True))
    lib=Lib(); monkeypatch.setattr(seccomp.ctypes, "CDLL", lambda name: lib)
    with pytest.raises(RuntimeError): seccomp.install()
    if failure != "init": assert lib.released
