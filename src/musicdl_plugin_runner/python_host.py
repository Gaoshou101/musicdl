"""Restricted, one-shot Python plugin host."""
from __future__ import annotations
import json, os, sys, tempfile
from typing import Any
try: import resource
except ImportError: resource = None
from musicdl.contracts.plugin import PluginInvocation, PluginResponse, PluginStep
from musicdl_plugin_runner import seccomp
_BUILTINS={name:getattr(__builtins__ if not isinstance(__builtins__,dict) else type("B",(),__builtins__),name) for name in "abs all any bool dict enumerate float int len list max min range reversed round sorted str sum tuple zip Exception ValueError".split()}
_LIMITS=(("RLIMIT_CPU",5,5),("RLIMIT_AS",256*1024*1024,256*1024*1024),("RLIMIT_FSIZE",1024*1024,1024*1024),("RLIMIT_NOFILE",32,32),("RLIMIT_CORE",0,0))
def build_command(_invocation):
 from musicdl_plugin_runner.supervisor import HostCommand
 return HostCommand([sys.executable,"-I","-m","musicdl_plugin_runner.python_host"],{})
def _error(inv,code,message):
    from musicdl.contracts.plugin import PluginError
    return PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1",request_id=inv.request.request_id,operation=inv.request.operation,ok=False,error=PluginError(code=code,message=message)))
def _run(inv):
    try:
        if resource is None: return _error(inv,"sandbox_unavailable","plugin sandbox unavailable")
        for name,soft,hard in _LIMITS: resource.setrlimit(getattr(resource,name),(soft,hard))
        os.environ.clear(); os.chdir(tempfile.mkdtemp(prefix="musicdl-plugin-"))
        # Install the kernel policy before compiling or evaluating attacker source.
        try:
            available = seccomp.install()
        except Exception:
            return _error(inv,"sandbox_unavailable","plugin sandbox unavailable")
        if not available: return _error(inv,"sandbox_unavailable","plugin sandbox unavailable")
        namespace:dict[str,Any]={"__builtins__":_BUILTINS}
        exec(compile(inv.source,"<plugin>","exec"),namespace,namespace)
        handle=namespace.get("handle")
        if not callable(handle): return _error(inv,"invalid_plugin","plugin handle is unavailable")
        value=handle(inv.request.model_dump(mode="json"))
        if isinstance(value,PluginStep): return value
        if isinstance(value,PluginResponse): return PluginStep(response=value)
        return PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1",request_id=inv.request.request_id,operation=inv.request.operation,ok=True,result=value))
    except MemoryError: return _error(inv,"resource_limit","plugin resource limit exceeded")
    except BaseException: return _error(inv,"plugin_error","plugin execution failed")
def main():
    try: step=_run(PluginInvocation.model_validate_json(sys.stdin.buffer.read(6*1024*1024+1)))
    except BaseException: return 1
    sys.stdout.write(step.model_dump_json()); sys.stdout.flush(); return 0
if __name__=="__main__": raise SystemExit(main())
