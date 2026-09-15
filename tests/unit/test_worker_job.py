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
        super().__init__(messages); self.pending=pending or []; self.retries={}; self.dead_letters=[]; self.deleted=[]; self.expiries=[]
    async def xautoclaim(self,*args,**kwargs): messages,self.pending=self.pending,[]; return ("0-0",messages)
    async def xreadgroup(self,g,c,streams,**k): messages,self.messages=self.messages,[]; return [(next(iter(streams)),messages)] if messages else []
    async def hincrby(self,key,field,amount): self.retries[key]=self.retries.get(key,0)+amount; return self.retries[key]
    async def expire(self,key,seconds=None): self.expiries.append((key,seconds)); return True
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
def cand(item="id"): return Candidate(source_id="src",source_version="1",item_id=item,title="Song",artist="Artist")
def test_selection_numeric_reply_consumes_and_acks(monkeypatch):
    calls=[]
    async def get(*a,**k): calls.append((a,k)); return {"token":"tok","corp_id":"c","from_user":"u","request_id":"r","version":"v","query":"q","generation":0,"candidates":{"2":cand().model_dump(mode="json")}}
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get); st=State(); raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"select","value":2,"msg_type":"text"}}).encode(); redis=Redis([("1-0",{b"payload":raw})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=st)
    assert run(w.run_selection_once())==1 and st.consumed[0][2:]==(2,86400) and len(redis.acks)==1
    assert calls[0][1]=={"namespace":"{tenant}"}
    assert st.consumed[0][1].query=="q" and st.consumed[0][1].candidates=={2:cand()}
def test_selection_invalid_or_missing_is_acked(monkeypatch):
    async def get(*a,**k): return None
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get); redis=Redis([("1-0",{b"payload":json.dumps({"content":"bad","from_user":"u","corp_id":"c"}).encode()})]); w=JobWorker(redis,WeCom(),{},"/tmp",state=State())
    assert run(w.run_selection_once())==0 and len(redis.acks)==1
def test_job_route_resolves_candidate_and_downloads(monkeypatch):
    calls=[]
    async def route(*a,**k): calls.append((a,k)); return {"from_user":"u","query":"q","version":"v","generation":0,"candidates":{"2":cand().model_dump(mode="json")}}
    async def download(c,*args,**kwargs): assert c.item_id=="id" and kwargs["request_id"]=="r"; return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.get_selection_for_request",route); monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download); wc=WeCom()
    result=run(JobWorker(Redis(),wc,{},"/tmp",state=State(),refresh=lambda *a:None).handle_job({"request_id":"r","index":"2"},job_id="1-0"))
    assert result.download.relative_path=="Song.mp3" and wc.sent==[("u","下载成功：Song.mp3")]
    assert calls[0][1]=={"namespace":"{tenant}"}
    assert len(calls)==1

def test_handle_user_selection_uses_state_namespace(monkeypatch):
    calls=[]
    async def get(*a,**k):
        calls.append((a,k)); return {"corp_id":"c","from_user":"u","request_id":"r","version":"v","query":"q","generation":0,"candidates":{"1":cand().model_dump(mode="json")}}
    monkeypatch.setattr("musicdl.worker.workers._get_by_token",get); st=State()
    run(JobWorker(Redis(),WeCom(),{},"/tmp",state=st,job_ttl=222).handle_user_selection("tok",1))
    assert calls[0][1]=={"namespace":"{tenant}"} and st.consumed[0][3]==222
@pytest.mark.parametrize("ttl", [59,604801,True])
def test_job_ttl_is_validated(ttl):
    with pytest.raises(ValueError,match="job ttl"):
        JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),job_ttl=ttl)
def test_job_direct_candidate_failure_notifies(monkeypatch):
    async def download(*a,**k): return SimpleNamespace(download=None,download_error="not_found",refresh_error="refresh_failed")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download); wc=WeCom(); run(JobWorker(Redis(),wc,{},"/tmp",state=State(),refresh=lambda *a:None).handle_job({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")},job_id="1-0"))
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
    try: run(JobWorker(Redis(),WeCom(),{},"/tmp",state=State()).handle_job({"request_id":"r","candidate":cand().model_dump(mode="json")},job_id="1-0"))
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
    async def get(*_a,**_k): return {"token":"tok","corp_id":"c","from_user":"u","request_id":"r","version":"v","query":"q","generation":0,"candidates":{"1":cand().model_dump(mode="json")}}
    class Broken(StreamRedis):
        async def delete(self,*_a,**_k): raise RuntimeError("delete unavailable")
    monkeypatch.setattr("musicdl.worker.workers.get_user_selection",get)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"select","value":1}}).encode()
    redis=Broken(messages=[("1-0",{b"payload":raw})]); worker=JobWorker(redis,WeCom(),{},"/tmp",state=State(),max_attempts=1)
    key=worker._retry_key(worker.selection_stream,"1-0"); redis.retries[key]=1
    run(worker.run_selection_once())
    assert redis.acks[-1][2]=="1-0" and key in redis.retries and redis.dead_letters==[]

def test_default_job_consumers_are_unique_and_bounded():
    first = JobWorker(Redis(), WeCom(), {}, "/tmp", state=State())
    second = JobWorker(Redis(), WeCom(), {}, "/tmp", state=State())
    assert first.consumer != second.consumer
    assert 1 <= len(first.consumer) <= 128
    assert 1 <= len(second.consumer) <= 128

def test_explicit_job_consumer_is_preserved():
    worker = JobWorker(Redis(), WeCom(), {}, "/tmp", state=State(), consumer="worker-explicit")
    assert worker.consumer == "worker-explicit"

def test_job_pending_lease_exceeds_job_timeout_and_download_is_bounded(monkeypatch):
    async def slow_download(*_args, **_kwargs):
        await asyncio.sleep(0.2)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback", slow_download)
    worker = JobWorker(Redis(), WeCom(), {}, "/tmp", state=State(), job_timeout=0.05, pending_idle_ms=51, refresh=lambda *_args: None)
    assert worker.pending_idle_ms > worker.job_timeout * 1000
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(worker.handle_job({"candidate": cand(), "request_id": "r", "query": "q"}, job_id="1-0"))


@pytest.mark.parametrize("job_id",["",None,7,"x"*257])
def test_handle_job_requires_an_explicit_valid_stream_id(job_id):
    worker=JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None)
    with pytest.raises(ValueError,match="invalid job id"):
        run(worker.handle_job({"request_id":"r","candidate":cand().model_dump(mode="json")},job_id=job_id))

def test_run_once_forwards_the_stream_id_as_the_job_id(monkeypatch):
    seen=[]
    async def download(*_a,**_k): return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    raw=json.dumps({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}).encode()
    redis=Redis([("7-3",{b"payload":raw})]); worker=JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None)
    original=worker.handle_job
    async def spy(job,**kwargs): seen.append(kwargs.get("job_id")); return await original(job,**kwargs)
    monkeypatch.setattr(worker,"handle_job",spy)
    assert run(worker.run_once())==1 and seen==["7-3"] and redis.acks[-1][2]=="7-3"

def test_stream_candidate_is_immutable_and_the_route_is_never_read(monkeypatch):
    lookups=[]; downloaded=[]
    async def route(*a,**k):
        lookups.append(a); return {"from_user":"attacker","query":"other","candidates":{"2":cand("other").model_dump(mode="json")}}
    async def download(c,*_a,**_k):
        downloaded.append(c.item_id); return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.get_selection_for_request",route)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    raw=json.dumps({"request_id":"r","from_user":"u","query":"q","candidate":cand("id").model_dump(mode="json")}).encode()
    redis=Redis([("1-0",{b"payload":raw})]); wc=WeCom()
    assert run(JobWorker(redis,wc,{},"/tmp",state=State(),refresh=lambda *_a:None).run_once())==1
    assert lookups==[] and downloaded==["id"] and wc.sent==[("u","下载成功：Song.mp3")]

def test_legacy_job_freezes_exactly_one_route_snapshot(monkeypatch):
    lookups=[]; downloaded=[]
    async def route(*a,**k):
        lookups.append(a); return {"from_user":"u","query":"q","candidates":{"2":cand("id").model_dump(mode="json")}}
    async def download(c,*_a,**_k):
        downloaded.append(c.item_id); return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.get_selection_for_request",route)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    raw=json.dumps({"request_id":"r","index":"2","from_user":"u"}).encode()
    redis=Redis([("1-0",{b"payload":raw})]); wc=WeCom()
    assert run(JobWorker(redis,wc,{},"/tmp",state=State(),refresh=lambda *_a:None).run_once())==1
    assert len(lookups)==1 and downloaded==["id"] and wc.sent==[("u","下载成功：Song.mp3")]

def test_job_string_candidate_is_parsed_and_malformed_json_is_rejected(monkeypatch):
    seen=[]
    async def download(c,*_a,**_k): seen.append(c.item_id); return SimpleNamespace(download=None,download_error="not_found",refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None)
    run(worker.handle_job({"request_id":"r","candidate":json.dumps(cand().model_dump(mode="json"))},job_id="1-0"))
    assert seen==["id"]
    with pytest.raises(json.JSONDecodeError):
        run(worker.handle_job({"request_id":"r","candidate":"{not json"},job_id="1-0"))

def test_job_passes_the_configured_sub_budgets_to_the_fallback(monkeypatch):
    seen={}
    async def download(c,*_a,**k): seen.update(k); seen["candidate"]=c; return SimpleNamespace(download=None,download_error="not_found",refresh_error=None)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None,
                     resolve_stream_timeout=15,refresh_timeout=4,health_timeout=5)
    assert (worker.resolve_stream_timeout,worker.refresh_timeout,worker.health_timeout)==(15.0,4.0,5.0)
    run(worker.handle_job({"request_id":"r","candidate":cand().model_dump(mode="json")},job_id="1-0"))
    assert (seen["resolve_stream_timeout"],seen["refresh_timeout"],seen["health_timeout"])==(15.0,4.0,5.0)
    assert seen["request_id"]=="r" and seen["candidate"].item_id=="id"

def test_job_sub_budgets_default_to_one_health_budget_and_unset_resolve_budgets():
    worker=JobWorker(Redis(),WeCom(),{},"/tmp",state=State())
    assert (worker.resolve_stream_timeout,worker.refresh_timeout,worker.health_timeout)==(None,None,10.0)

@pytest.mark.parametrize("kwargs",[{"resolve_stream_timeout":0},{"resolve_stream_timeout":-1.0},{"resolve_stream_timeout":True},
                                   {"resolve_stream_timeout":float("nan")},{"refresh_timeout":0},{"refresh_timeout":float("inf")},
                                   {"health_timeout":0},{"health_timeout":-2},{"health_timeout":True}])
def test_job_sub_budgets_are_strictly_validated(kwargs):
    with pytest.raises(ValueError,match="invalid"):
        JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),**kwargs)

@pytest.mark.parametrize("value",[0,-1,True,604801,None,"600",604800.0])
def test_job_retry_window_is_strictly_validated(value):
    with pytest.raises(ValueError,match="invalid retry window"):
        JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),retry_window_seconds=value)

def test_job_failure_uses_the_configured_retry_window(monkeypatch):
    async def fail(*_a,**_k): raise RuntimeError("private download failure")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",fail)
    raw=json.dumps({"request_id":"r","from_user":"u","candidate":cand().model_dump(mode="json")}).encode()
    redis=StreamRedis(messages=[("1-0",{b"payload":raw})])
    worker=JobWorker(redis,WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None,max_attempts=3,retry_window_seconds=3600)
    assert run(worker.run_once())==0 and redis.acks==[]
    assert redis.expiries==[(worker._retry_key(worker.stream,"1-0"),3600)]

def test_message_worker_failure_uses_the_configured_retry_window(monkeypatch):
    from musicdl.worker.workers import MessageWorker
    async def boom(*_a,**_k): raise RuntimeError("search exploded")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",boom)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","command":"search","value":"song"}).encode()
    redis=StreamRedis(messages=[("1-0",{b"payload":raw})])
    state=State(); state.namespace="{tenant}"; state.message_stream="messages"
    worker=MessageWorker(redis,object(),WeCom(),state=state,search_timeout=1.0,pending_idle_ms=30001,
                         max_attempts=3,retry_window_seconds=7200)
    assert run(worker.run_once())==0 and redis.acks==[]
    assert redis.expiries==[(worker._retry_key(worker.stream,"1-0"),7200)]
