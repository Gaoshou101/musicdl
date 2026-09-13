import asyncio, json, pytest
from types import SimpleNamespace
from musicdl.sources.models import Candidate
from musicdl.worker.workers import JobWorker

class Redis:
    def __init__(self,messages=None): self.messages=messages or []; self.acks=[]
    async def xgroup_create(self,*a,**k): pass
    async def xreadgroup(self,g,c,streams,**k): return [(next(iter(streams)),self.messages)]
    async def xack(self,s,g,m): self.acks.append((s,g,m))
    async def get(self,k): return None
class StreamRedis(Redis):
    def __init__(self,messages=None,pending=None):
        super().__init__(messages); self.pending=pending or []; self.retries={}; self.dead_letters=[]; self.deleted=[]
    async def xautoclaim(self,*args,**kwargs): messages,self.pending=self.pending,[]; return ("0-0",messages)
    async def xreadgroup(self,g,c,streams,**k): messages,self.messages=self.messages,[]; return [(next(iter(streams)),messages)] if messages else []
    async def hincrby(self,key,field,amount): self.retries[key]=self.retries.get(key,0)+amount; return self.retries[key]
    async def expire(self,*_a,**_k): return True
    async def delete(self,key): self.deleted.append(key); self.retries.pop(key,None); return 1
    async def xadd(self,stream,fields,**kwargs): self.dead_letters.append((stream,fields,kwargs)); return "9-0"
class State:
    job_stream="jobs"; message_stream="messages"; namespace="{tenant}"
    def __init__(self): self.consumed=[]
    async def consume_selection(self,t,c,i,ttl=86400): self.consumed.append((t,c,i,ttl)); return SimpleNamespace(job_id="j")
class WeCom:
    def __init__(self): self.sent=[]
    async def send_text(self,u,c): self.sent.append((u,c)); return {}
def run(c): return asyncio.run(c)
def cand(): return Candidate(source_id="src",source_version="1",item_id="id",title="Song",artist="Artist")
def test_selection_numeric_reply_consumes_and_acks(monkeypatch):
    calls=[]
    async def get(*a,**k): calls.append((a,k)); return {"token":"tok","corp_id":"c","from_user":"u","request_id":"r","version":"v","candidates":{"2":"id"}}
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get); st=State(); raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"select","value":2,"msg_type":"text"}}).encode(); redis=Redis([("1-0",{b"payload":raw})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=st)
    assert run(w.run_selection_once())==1 and st.consumed[0][2:]==(2,86400) and len(redis.acks)==1
    assert calls[0][1]=={"namespace":"{tenant}"}
def test_selection_invalid_or_missing_is_acked(monkeypatch):
    async def get(*a,**k): return None
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get); redis=Redis([("1-0",{b"payload":json.dumps({"content":"bad","from_user":"u","corp_id":"c"}).encode()})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=State())
    assert run(w.run_selection_once())==0 and len(redis.acks)==1
def test_job_route_resolves_candidate_and_downloads(monkeypatch):
    calls=[]
    async def route(*a,**k): calls.append((a,k)); return {"from_user":"u","query":"q","candidates":{"2":cand().model_dump(mode="json")}}
    async def download(c,*args,**kwargs): assert c.item_id=="id" and kwargs["request_id"]=="r"; return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.get_selection_for_request",route); monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download); wc=WeCom()
    result=run(JobWorker(Redis(),wc,{},"/tmp",state=State(),refresh=lambda *a:None).handle_job({"request_id":"r","index":"2"}))
    assert result.download.relative_path=="Song.mp3" and wc.sent==[("u","下载成功：Song.mp3")]
    assert calls[0][1]=={"namespace":"{tenant}"}

def test_handle_user_selection_uses_state_namespace(monkeypatch):
    calls=[]
    async def get(*a,**k):
        calls.append((a,k)); return {"corp_id":"c","from_user":"u","request_id":"r","version":"v","candidates":{"1":"id"}}
    monkeypatch.setattr("musicdl.worker.workers._get_by_token",get); st=State()
    run(JobWorker(Redis(),WeCom(),{},"/tmp",state=st,job_ttl=222).handle_user_selection("tok",1))
    assert calls[0][1]=={"namespace":"{tenant}"} and st.consumed[0][3]==222
@pytest.mark.parametrize("ttl", [59,604801,True])
def test_job_ttl_is_validated(ttl):
    with pytest.raises(ValueError,match="job ttl"):
        JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),job_ttl=ttl)
def test_job_direct_candidate_failure_notifies(monkeypatch):
    async def download(*a,**k): return SimpleNamespace(download=None,download_error="not_found",refresh_error="refresh_failed")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download); wc=WeCom(); run(JobWorker(Redis(),wc,{},"/tmp",state=State(),refresh=lambda *a:None).handle_job({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}))
    assert wc.sent==[("u","下载失败，已重试：not_found")]
def test_job_bad_json_is_acked():
    redis=Redis([("1-0",{b"payload":b"bad"})]); assert run(JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *a:None).run_once())==0 and len(redis.acks)==1
@pytest.mark.parametrize("raw", [b"[]",b"null"])
def test_job_non_mapping_payload_is_bad_message_and_acked(raw):
    redis=StreamRedis([("1-0",{b"payload":raw})])
    assert run(JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None).run_once())==0
    assert redis.acks[-1][2]=="1-0" and redis.retries=={}
def test_job_download_exception_is_not_acked(monkeypatch):
    async def fail(*a,**k): raise RuntimeError("crashed")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",fail); raw=json.dumps({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}).encode(); redis=Redis([("1-0",{b"payload":raw})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *a:None)
    assert run(w.run_once())==0 and redis.acks==[]
def test_job_requires_refresh_callback():
    try: run(JobWorker(Redis(),WeCom(),{},"/tmp",state=State()).handle_job({"request_id":"r","candidate":cand().model_dump(mode="json")}))
    except RuntimeError as exc: assert "refresh" in str(exc)
    else: raise AssertionError("missing refresh must fail")

def test_job_failure_retries_then_dead_letters_notifies_and_acks(monkeypatch):
    async def fail(*_a,**_k): raise RuntimeError("private download failure")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",fail)
    raw=json.dumps({"request_id":"r","from_user":"job-user","query":"private query","candidate":cand().model_dump(mode="json")}).encode()
    message=("1-0",{b"payload":raw}); redis=StreamRedis(messages=[message]); wc=WeCom()
    worker=JobWorker(redis,wc,{},"/tmp",state=State(),refresh=lambda *_a:None,max_attempts=2)
    assert run(worker.run_once())==0 and redis.acks==[] and list(redis.retries.values())==[1]
    redis.pending=[message]
    assert run(worker.run_once())==0 and redis.acks[-1][2]=="1-0"
    stream,fields,_=redis.dead_letters[0]
    assert stream=="{tenant}:stream:dead-letter" and "private query" not in json.dumps(fields) and "private download failure" not in json.dumps(fields)
    assert wc.sent==[("job-user","处理失败，请稍后重试。")]
    assert redis.retries=={} and redis.deleted

def test_job_run_forever_listens_to_selection_and_jobs_and_propagates_cancellation():
    class Dual(JobWorker):
        def __init__(self,*a,**k): super().__init__(*a,**k); self.selection_started=asyncio.Event(); self.jobs_started=asyncio.Event()
        async def run_selection_once(self): self.selection_started.set(); await asyncio.Event().wait()
        async def run_once(self): self.jobs_started.set(); await asyncio.Event().wait()
    async def scenario():
        worker=Dual(Redis(),WeCom(),{},"/tmp",state=State())
        task=asyncio.create_task(worker.run_forever(poll_interval=0.01))
        await asyncio.wait_for(asyncio.gather(worker.selection_started.wait(),worker.jobs_started.wait()),1)
        task.cancel()
        try: await task
        except asyncio.CancelledError: return True
        return False
    assert run(scenario()) is True

def test_job_run_forever_cancels_sibling_when_one_loop_fails():
    class Broken(JobWorker):
        def __init__(self,*a,**k): super().__init__(*a,**k); self.sibling_cancelled=False
        async def run_selection_once(self): raise RuntimeError("selection crashed")
        async def run_once(self):
            try: await asyncio.Event().wait()
            finally: self.sibling_cancelled=True
    worker=Broken(Redis(),WeCom(),{},"/tmp",state=State())
    with pytest.raises(ExceptionGroup): run(worker.run_forever(poll_interval=0.01))
    assert worker.sibling_cancelled is True

@pytest.mark.parametrize("interval", [float("nan"),float("inf"),float("-inf")])
def test_job_run_forever_rejects_non_finite_poll_interval(interval):
    with pytest.raises(ValueError,match="poll interval"):
        run(JobWorker(Redis(),WeCom(),{},"/tmp",state=State()).run_forever(poll_interval=interval))

@pytest.mark.parametrize("kwargs", [{"pending_idle_ms":0},{"max_attempts":0},{"max_attempts":101}])
def test_job_delivery_settings_are_strictly_validated(kwargs):
    with pytest.raises(ValueError):
        JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),**kwargs)

def test_job_success_delete_failure_is_not_recorded_as_business_failure(monkeypatch):
    async def download(*_a,**_k): return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    class Broken(StreamRedis):
        async def delete(self,*_a,**_k): raise RuntimeError("delete unavailable")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    raw=json.dumps({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}).encode()
    redis=Broken(messages=[("1-0",{b"payload":raw})]); worker=JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None,max_attempts=1)
    key=worker._retry_key(worker.stream,"1-0"); redis.retries[key]=1
    run(worker.run_once())
    assert redis.acks[-1][2]=="1-0" and key in redis.retries and redis.dead_letters==[]

def test_job_selection_success_delete_failure_is_not_recorded(monkeypatch):
    async def get(*_a,**_k): return {"token":"tok","corp_id":"c","from_user":"u","request_id":"r","version":"v","candidates":{"1":"id"}}
    class Broken(StreamRedis):
        async def delete(self,*_a,**_k): raise RuntimeError("delete unavailable")
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"select","value":1}}).encode()
    redis=Broken(messages=[("1-0",{b"payload":raw})]); worker=JobWorker(redis,WeCom(),{},"/tmp",state=State(),max_attempts=1)
    key=worker._retry_key(worker.selection_stream,"1-0"); redis.retries[key]=1
    run(worker.run_selection_once())
    assert redis.acks[-1][2]=="1-0" and key in redis.retries and redis.dead_letters==[]
