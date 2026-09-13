import asyncio, json
from types import SimpleNamespace
from musicdl.sources.models import Candidate
from musicdl.worker.workers import JobWorker

class Redis:
    def __init__(self,messages=None): self.messages=messages or []; self.acks=[]
    async def xgroup_create(self,*a,**k): pass
    async def xreadgroup(self,g,c,streams,**k): return [(next(iter(streams)),self.messages)]
    async def xack(self,s,g,m): self.acks.append((s,g,m))
    async def get(self,k): return None
class State:
    job_stream="jobs"; message_stream="messages"
    def __init__(self): self.consumed=[]
    async def consume_selection(self,t,c,i): self.consumed.append((t,c,i)); return SimpleNamespace(job_id="j")
class WeCom:
    def __init__(self): self.sent=[]
    async def send_text(self,u,c): self.sent.append((u,c)); return {}
def run(c): return asyncio.run(c)
def cand(): return Candidate(source_id="src",source_version="1",item_id="id",title="Song",artist="Artist")
def test_selection_numeric_reply_consumes_and_acks(monkeypatch):
    async def get(*a,**k): return {"token":"tok","corp_id":"c","from_user":"u","request_id":"r","version":"v","candidates":{"2":"id"}}
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get); st=State(); raw=json.dumps({"content":"2","from_user":"u","corp_id":"c"}).encode(); redis=Redis([("1-0",{b"payload":raw})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=st)
    assert run(w.run_selection_once())==1 and st.consumed[0][2]==2 and len(redis.acks)==1
def test_selection_invalid_or_missing_is_acked(monkeypatch):
    async def get(*a,**k): return None
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get); redis=Redis([("1-0",{b"payload":json.dumps({"content":"bad","from_user":"u","corp_id":"c"}).encode()})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=State())
    assert run(w.run_selection_once())==0 and len(redis.acks)==1
def test_job_route_resolves_candidate_and_downloads(monkeypatch):
    async def route(*a,**k): return {"from_user":"u","query":"q","candidates":{"2":cand().model_dump(mode="json")}}
    async def download(c,*args,**kwargs): assert c.item_id=="id" and kwargs["request_id"]=="r"; return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.get_selection_for_request",route); monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download); wc=WeCom()
    result=run(JobWorker(Redis(),wc,{},"/tmp",state=State(),refresh=lambda *a:None).handle_job({"request_id":"r","index":"2"}))
    assert result.download.relative_path=="Song.mp3" and wc.sent==[("u","下载成功：Song.mp3")]
def test_job_direct_candidate_failure_notifies(monkeypatch):
    async def download(*a,**k): return SimpleNamespace(download=None,download_error="not_found",refresh_error="refresh_failed")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download); wc=WeCom(); run(JobWorker(Redis(),wc,{},"/tmp",state=State(),refresh=lambda *a:None).handle_job({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}))
    assert wc.sent==[("u","下载失败，已重试：not_found")]
def test_job_bad_json_is_acked():
    redis=Redis([("1-0",{b"payload":b"bad"})]); assert run(JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *a:None).run_once())==0 and len(redis.acks)==1
def test_job_download_exception_is_not_acked(monkeypatch):
    async def fail(*a,**k): raise RuntimeError("crashed")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",fail); raw=json.dumps({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}).encode(); redis=Redis([("1-0",{b"payload":raw})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *a:None)
    assert run(w.run_once())==0 and redis.acks==[]
def test_job_requires_refresh_callback():
    try: run(JobWorker(Redis(),WeCom(),{},"/tmp",state=State()).handle_job({"request_id":"r","candidate":cand().model_dump(mode="json")}))
    except RuntimeError as exc: assert "refresh" in str(exc)
    else: raise AssertionError("missing refresh must fail")
