import asyncio, json, time, pytest
from types import SimpleNamespace
from test_wecom_state import ID3, ScriptRedis, Source, playable_metadata
from musicdl.media.models import FallbackResult
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult
from musicdl.wecom.state import RedisStateStore
from musicdl.worker.workers import FALLBACK_NOTICE, TERMINAL_FAILURE_TEXT, JobDeferred, JobWorker

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
class State(RedisStateStore):
    """The real state store over the shared script fake, plus a recorded selection consume."""
    def __init__(self, redis=None):
        self.script = redis if redis is not None else ScriptRedis()
        super().__init__(self.script, namespace="{tenant}", message_stream="messages", job_stream="jobs")
        self.consumed=[]
    async def consume_selection(self,t,c,i,*,ttl=86400): self.consumed.append((t,c,i,ttl)); return SimpleNamespace(job_id="j")
class WeCom:
    def __init__(self): self.sent=[]
    async def send_text(self,u,c): self.sent.append((u,c)); return {}
def run(c): return asyncio.run(c)
def cand(item="id"): return Candidate(source_id="src",source_version="1",item_id=item,title="Song",artist="Artist",format="mp3")
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
    async def download(c,*args,**kwargs): assert c.item_id=="id" and kwargs["request_id"]=="r"; return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None,refreshed=None)
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
    async def download(*a,**k): return SimpleNamespace(download=None,download_error="not_found",refresh_error="refresh_failed",refreshed=None)
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
    async def download(*_a,**_k): return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None,refreshed=None)
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
        downloaded.append(c.item_id); return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None,refreshed=None)
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
        downloaded.append(c.item_id); return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,refresh_error=None,refreshed=None)
    monkeypatch.setattr("musicdl.worker.workers.get_selection_for_request",route)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    raw=json.dumps({"request_id":"r","index":"2","from_user":"u"}).encode()
    redis=Redis([("1-0",{b"payload":raw})]); wc=WeCom()
    assert run(JobWorker(redis,wc,{},"/tmp",state=State(),refresh=lambda *_a:None).run_once())==1
    assert len(lookups)==1 and downloaded==["id"] and wc.sent==[("u","下载成功：Song.mp3")]

def test_job_string_candidate_is_parsed_and_malformed_json_is_rejected(monkeypatch):
    seen=[]
    async def download(c,*_a,**_k): seen.append(c.item_id); return SimpleNamespace(download=None,download_error="not_found",refresh_error=None,refreshed=None)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),WeCom(),{},"/tmp",state=State(),refresh=lambda *_a:None)
    run(worker.handle_job({"request_id":"r","candidate":json.dumps(cand().model_dump(mode="json"))},job_id="1-0"))
    assert seen==["id"]
    with pytest.raises(json.JSONDecodeError):
        run(worker.handle_job({"request_id":"r","candidate":"{not json"},job_id="1-0"))

def test_job_passes_the_configured_sub_budgets_to_the_fallback(monkeypatch):
    seen={}
    async def download(c,*_a,**k): seen.update(k); seen["candidate"]=c; return SimpleNamespace(download=None,download_error="not_found",refresh_error=None,refreshed=None)
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

KIND="欧美/Artist/Song - Artist.mp3"

def job_payload(**extra):
    payload={"request_id":"r","from_user":"u","corp_id":"c","query":"q","generation":0,
             "candidate":cand().model_dump(mode="json")}
    payload.update(extra)
    return payload

def job_message(payload=None,message_id="1-0"):
    raw=json.dumps(payload if payload is not None else job_payload()).encode()
    return StreamRedis(messages=[(message_id,{b"payload":raw})])

def effect(state,job_id,name):
    return state.script.hashes.get(f"{{tenant}}:effect:{job_id}:{name}")

def effect_result(state,job_id,name):
    raw=(effect(state,job_id,name) or {}).get("result")
    return json.loads(raw) if raw else None

def seed_effect(state,job_id,name,*,owner="owner-1",fence=1,status="done",result='{"ok":true}',stage="completed"):
    state.script.hashes[f"{{tenant}}:effect:{job_id}:{name}"]={"job_id":job_id,"effect":name,"owner":owner,
        "fence":str(fence),"stage":stage,"status":status,"lease_until_ms":"","result":result}

def refresh_result(count=1,version="v2"):
    return SearchResult(candidates=tuple(cand(item=f"n{index}") for index in range(1,count+1)),
                        statuses=(),version=version)

def ok_download():
    return SimpleNamespace(download=SimpleNamespace(relative_path="Song.mp3"),download_error=None,
                           refresh_error=None,refreshed=None)

def test_job_claims_the_download_effect_and_reserves_before_the_source_call(monkeypatch,tmp_path):
    st=State(); seen={}
    async def download(candidate,sources,root,**kwargs):
        seen["stage"]=effect(st,"1-0","download")["stage"]
        seen["artifact"]=dict(st.script.hashes.get("{tenant}:artifact:1-0") or {})
        seen.update({name:kwargs.get(name) for name in ("reservation","artifact_store","owner","fence")})
        return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),WeCom(),{},str(tmp_path),state=st,refresh=lambda *a:None)
    result=run(worker.handle_job(job_payload(),job_id="1-0"))
    assert result.download.relative_path=="Song.mp3"
    assert seen["stage"]=="external_started" and seen["artifact"]["state"]=="prepared"
    assert seen["artifact"]["extension"]==".mp3" and seen["reservation"].extension==".mp3"
    assert seen["artifact_store"] is st and seen["reservation"].candidate_id=="id"
    assert seen["reservation"].target_relative_path==KIND
    record=effect(st,"1-0","download")
    assert (seen["owner"],seen["fence"])==(record["owner"],int(record["fence"])) and record["status"]=="done"

def test_live_lease_delivery_stays_pending_without_retry_or_ack(monkeypatch):
    st=State(); calls=[]
    async def download(*_a,**_k): calls.append(1); return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    seed_effect(st,"1-0","download",owner="other-worker",status="running",result="",stage="claimed")
    st.script.hashes["{tenant}:effect:1-0:download"]["lease_until_ms"]=str(st.script.now+60000)
    redis=job_message()
    worker=JobWorker(redis,WeCom(),{},"/tmp",state=st,refresh=lambda *a:None,max_attempts=1)
    assert run(worker.run_once())==0
    assert calls==[] and redis.acks==[] and redis.dead_letters==[] and redis.retries=={}

def test_job_dead_letters_only_after_the_configured_attempts(monkeypatch):
    async def boom(*_a,**_k): raise RuntimeError("private download failure")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",boom)
    class Replay(StreamRedis):
        async def xreadgroup(self,g,c,streams,**k): return [(next(iter(streams)),self.messages)]
    raw=json.dumps(job_payload()).encode()
    redis=Replay(messages=[("1-0",{b"payload":raw})]); wc=WeCom()
    worker=JobWorker(redis,wc,{},"/tmp",state=State(),refresh=lambda *a:None,max_attempts=2)
    assert run(worker.run_once())==0
    assert redis.dead_letters==[] and redis.acks==[] and redis.retries=={worker._retry_key(worker.stream,"1-0"):1}
    assert run(worker.run_once())==0
    assert len(redis.dead_letters)==1 and len(redis.acks)==1
    retry_key=worker._retry_key(worker.stream,"1-0")
    assert redis.deleted==[retry_key] and retry_key not in redis.retries
    assert redis.dead_letters[0][1]["reason"]=="business_failure" and redis.dead_letters[0][1]["attempts"]=="2"
    assert wc.sent==[("u",TERMINAL_FAILURE_TEXT)]

def test_published_artifact_is_replayed_without_a_second_source_call(monkeypatch):
    st=State(); seen={}
    seed_effect(st,"1-0","download",owner="earlier",fence=1,result='{"ok":true}')
    st.script.hashes["{tenant}:artifact:1-0"]={"job_id":"1-0","candidate_id":"id",
        "temporary_relative_path":".musicdl-staging/a.1.part","target_relative_path":"未知/Artist/Song.mp3",
        "allocation_slot":"1","extension":".mp3","media_type":"audio/mpeg","size_bytes":"10",
        "sha256":"a"*64,"owner":"earlier","fence":"1","state":"published"}
    class Src:
        def __init__(self): self.calls=0
        async def download(self,candidate): self.calls+=1; return ok_download()
        async def health(self): return True
    src=Src(); wc=WeCom()
    async def download(candidate,sources,root,**kwargs):
        seen.update({name:kwargs.get(name) for name in ("reservation","artifact_store","owner","fence")})
        return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),wc,{"src":src},"/tmp",state=st,refresh=lambda *a:None)
    result=run(worker.handle_job(job_payload(),job_id="1-0"))
    assert result.download.relative_path=="Song.mp3" and wc.sent==[("u","下载成功：Song.mp3")]
    assert seen["reservation"].state=="published" and seen["reservation"].sha256=="a"*64
    assert seen["reservation"].size_bytes==10 and seen["artifact_store"] is st and seen["fence"]==1
    assert src.calls==0
    assert (effect(st,"1-0","download")["fence"],effect(st,"1-0","download")["status"])==("1","done")

def test_refreshed_candidates_rebind_one_generation_and_prompt_once(monkeypatch):
    st=State(); seen={}
    async def download(candidate,sources,root,**kwargs):
        return FallbackResult(download=None,download_error="source_failed",refreshed=refresh_result(2,"v2"))
    async def issue(self,context,ttl=600,max_attempts=5):
        seen["context"]=context; seen["ttl"]=ttl; return "tok-2"
    async def bind(redis,token,context,**kwargs): seen["bind"]=(redis,token,context,kwargs)
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    monkeypatch.setattr(State,"issue_selection",issue)
    monkeypatch.setattr("musicdl.worker.workers.bind_user_selection",bind)
    wc=WeCom()
    worker=JobWorker(Redis(),wc,{},"/tmp",state=st,refresh=lambda *a:None)
    result=run(worker.handle_job(job_payload(),job_id="1-0"))
    assert result.download is None and result.download_error=="source_failed"
    context=seen["context"]
    assert (context.selection_generation,context.candidate_set_version)==(1,"v2")
    assert list(context.candidates)==[1,2] and context.candidates[2].item_id=="n2"
    assert (context.corp_id,context.from_user,context.request_id,context.query)==("c","u","r","q")
    assert seen["ttl"]==600 and seen["bind"][1]=="tok-2" and seen["bind"][3]["namespace"]=="{tenant}"
    assert effect(st,"1-0","rebind")["status"]=="done" and effect_result(st,"1-0","rebind")["generation"]==1
    assert len(wc.sent)==1 and wc.sent[0][0]=="u" and wc.sent[0][1].endswith("回复序号下载。")
    assert wc.sent[0][1].startswith(FALLBACK_NOTICE)
    assert wc.sent[0][1].count("Song — Artist")==2
    assert effect(st,"1-0","selection_prompt")["status"]=="done"

def test_uncertain_prompt_is_never_resent_and_the_job_still_completes(monkeypatch):
    st=State(); calls=[]
    async def download(candidate,sources,root,**kwargs):
        calls.append(1)
        return FallbackResult(download=None,download_error="source_failed",refreshed=refresh_result(1,"v2"))
    async def issue(self,context,ttl=600,max_attempts=5): return "tok-2"
    async def bind(*_a,**_k): return None
    class BoomWeCom:
        def __init__(self): self.sends=[]
        async def send_text(self,user,text): self.sends.append((user,text)); raise RuntimeError("wecom down")
    class Replay(StreamRedis):
        async def xreadgroup(self,g,c,streams,**k): return [(next(iter(streams)),self.messages)]
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    monkeypatch.setattr(State,"issue_selection",issue)
    monkeypatch.setattr("musicdl.worker.workers.bind_user_selection",bind)
    redis=Replay(messages=[("1-0",{b"payload":json.dumps(job_payload()).encode()})]); wc=BoomWeCom()
    worker=JobWorker(redis,wc,{},"/tmp",state=st,refresh=lambda *a:None)
    assert run(worker.run_once())==1 and len(redis.acks)==1
    assert effect_result(st,"1-0","selection_prompt")=={"code":"prompt_uncertain"}
    assert run(worker.run_once())==1 and len(redis.acks)==2
    prompts=[text for _,text in wc.sends if text.endswith("回复序号下载。")]
    assert len(prompts)==1 and len(calls)==1
    assert effect(st,"1-0","selection_prompt")["status"]=="uncertain"
    assert effect(st,"1-0","rebind")["status"]=="done"
    assert effect(st,"1-0","terminal_failure_notice")["status"]=="uncertain"

def test_partial_reservation_is_terminal_without_a_second_source_call(monkeypatch):
    st=State(); calls=[]
    st.script.hashes["{tenant}:artifact:1-0"]={"job_id":"1-0","candidate_id":"id",
        "temporary_relative_path":".musicdl-staging/a.2.part","target_relative_path":"未知/Artist/Song.mp3",
        "allocation_slot":"1","extension":".mp3","media_type":"audio/mpeg","owner":"earlier",
        "fence":"2","state":"external_started"}
    async def download(*_a,**_k): calls.append(1); return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    redis=job_message(); wc=WeCom()
    worker=JobWorker(redis,wc,{},"/tmp",state=st,refresh=lambda *a:None,max_attempts=1)
    assert run(worker.run_once())==0
    assert calls==[] and len(redis.dead_letters)==1 and len(redis.acks)==1
    assert redis.dead_letters[0][1]["reason"]=="business_failure"
    assert wc.sent==[("u",TERMINAL_FAILURE_TEXT)]

def test_notice_timeout_is_capped_by_the_remaining_handler_deadline(monkeypatch):
    budgets=[]; real_timeout=asyncio.timeout
    def spy(delay): budgets.append(delay); return real_timeout(delay)
    async def download(candidate,sources,root,**kwargs): return ok_download()
    class QuietWeCom:
        def __init__(self): self.sent=[]
        async def send_text(self,user,text): self.sent.append((user,text)); return {}
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    monkeypatch.setattr(asyncio,"timeout",spy)
    roomy=QuietWeCom(); tight=QuietWeCom()
    roomy_worker=JobWorker(Redis(),roomy,{},"/tmp",state=State(),refresh=lambda *a:None,
                           job_timeout=30.0,wecom_notice_timeout=10.0)
    run(roomy_worker.handle_job(job_payload(),job_id="1-0"))
    tight_worker=JobWorker(Redis(),tight,{},"/tmp",state=State(),refresh=lambda *a:None,
                           job_timeout=0.5,wecom_notice_timeout=10.0)
    run(tight_worker.handle_job(job_payload(),job_id="1-0"))
    assert len(budgets)==2 and budgets[0]==10.0 and 0.0<budgets[1]<=0.5
    assert roomy.sent==[("u","下载成功：Song.mp3")] and tight.sent==[("u","下载成功：Song.mp3")]

def test_hanging_notice_is_bounded_and_never_resent(monkeypatch):
    st=State(); now=time.monotonic()
    class SlowWeCom:
        def __init__(self): self.calls=0
        async def send_text(self,user,text): self.calls+=1; await asyncio.sleep(30)
    async def download(candidate,sources,root,**kwargs): return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    wc=SlowWeCom()
    worker=JobWorker(Redis(),wc,{},"/tmp",state=st,refresh=lambda *a:None,
                     job_timeout=5.0,wecom_notice_timeout=0.25)
    result=run(worker.handle_job(job_payload(),job_id="1-0"))
    assert result.download.relative_path=="Song.mp3"
    assert time.monotonic()-now<5 and wc.calls==1
    assert effect_result(st,"1-0","success_notice")=={"code":"success_notice_uncertain"}
    replay=run(worker.handle_job(job_payload(),job_id="1-0"))
    assert replay.download.relative_path=="Song.mp3" and wc.calls==1
    assert effect(st,"1-0","success_notice")["status"]=="uncertain"

def test_health_and_refresh_are_claimed_as_their_own_fenced_effects(monkeypatch):
    st=State(); seen={}
    class Src:
        def __init__(self): self.calls=0
        async def download(self,candidate): raise AssertionError("the source must not stream twice")
        async def health(self): self.calls+=1; return True
    src=Src()
    async def reload(query,excluded): seen["refresh_args"]=(query,excluded); return refresh_result(1,"v3")
    async def download(candidate,sources,root,**kwargs):
        seen["healthy"]=await sources["src"].health()
        seen["healthy_again"]=await sources["src"].health()
        seen["refreshed"]=await kwargs["refresh"]("q",frozenset())
        return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),WeCom(),{"src":src},"/tmp",state=st,refresh=reload)
    result=run(worker.handle_job(job_payload(),job_id="1-0"))
    assert result.download.relative_path=="Song.mp3"
    assert seen["healthy"] is True and seen["healthy_again"] is True and src.calls==1
    assert seen["refreshed"].version=="v3" and seen["refresh_args"]==("q",frozenset())
    assert st.script.hashes["{tenant}:effect:1-0:health"]["status"]=="done"
    assert effect_result(st,"1-0","health")=={"healthy":True,"ok":True}
    assert st.script.hashes["{tenant}:effect:1-0:refresh"]["status"]=="done"
    assert effect_result(st,"1-0","refresh")=={"candidates":1,"ok":True,"version":"v3"}
    assert st.script.hashes["{tenant}:effect:1-0:health"]["effect"]=="health"
    assert st.script.hashes["{tenant}:effect:1-0:refresh"]["effect"]=="refresh"


def reserved_download(monkeypatch,tmp_path,*,advisor=None,payload=None,state=None):
    """Run one job and report the reserved path and the language the engine saw."""
    st=state if state is not None else State(); seen={}
    async def download(candidate,sources,root,**kwargs):
        seen["target"]=dict(st.script.hashes.get("{tenant}:artifact:1-0") or {}).get("target_relative_path")
        seen["language"]=kwargs.get("language")
        return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback",download)
    worker=JobWorker(Redis(),WeCom(),{},str(tmp_path),state=st,refresh=lambda *a:None,
                     language_advisor=advisor)
    run(worker.handle_job(payload if payload is not None else job_payload(),job_id="1-0"))
    return seen

def test_the_candidate_metadata_decides_the_category_directory(monkeypatch,tmp_path):
    payload=job_payload(candidate=Candidate(source_id="src",source_version="1",item_id="id",
                                            title="七里香",artist="周杰伦",format="mp3").model_dump(mode="json"))
    seen=reserved_download(monkeypatch,tmp_path,payload=payload)
    assert seen["target"]=="华语/周杰伦/七里香 - 周杰伦.mp3" and seen["language"]=="华语"

def test_language_advice_overrides_the_deterministic_category(monkeypatch,tmp_path):
    async def advisor(candidate): return "日韩"
    seen=reserved_download(monkeypatch,tmp_path,advisor=advisor)
    assert seen["target"]=="日韩/Artist/Song - Artist.mp3" and seen["language"]=="日韩"

@pytest.mark.parametrize("advice",[None,"boom","junk"])
def test_unusable_language_advice_keeps_the_deterministic_category(monkeypatch,tmp_path,advice):
    async def advisor(candidate):
        if advice=="boom": raise RuntimeError("provider down")
        return advice
    seen=reserved_download(monkeypatch,tmp_path,advisor=None if advice is None else advisor)
    assert seen["target"]=="欧美/Artist/Song - Artist.mp3" and seen["language"]=="欧美"

def test_a_prepared_reservation_pins_the_category_directory(monkeypatch,tmp_path):
    st=State()
    st.script.hashes["{tenant}:artifact:1-0"]={"job_id":"1-0","candidate_id":"id",
        "temporary_relative_path":".musicdl-staging/a.3.part","target_relative_path":"未知/Artist/Song - Artist.mp3",
        "allocation_slot":"1","extension":".mp3","media_type":"audio/mpeg","owner":"earlier",
        "fence":"1","state":"prepared"}
    async def advisor(candidate): return "华语"
    seen=reserved_download(monkeypatch,tmp_path,advisor=advisor,state=st)
    assert seen["target"]=="未知/Artist/Song - Artist.mp3" and seen["language"]=="未知"
