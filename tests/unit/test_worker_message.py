import asyncio
import json
import pytest
from musicdl.ai.models import AIRankResult
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult
from musicdl.worker.workers import MessageWorker, _envelope

class Redis:
    def __init__(self, messages=None): self.messages=messages or []; self.acks=[]; self.groups=[]; self.values={}
    async def xgroup_create(self,s,g,**k): self.groups.append((s,g))
    async def xreadgroup(self,g,c,streams,**k): return [(next(iter(streams)),self.messages)]
    async def xack(self,s,g,m): self.acks.append((s,g,m))
    async def set(self,k,v,**kw): self.values[k]=v
    async def get(self,k): return self.values.get(k)

class StreamRedis(Redis):
    def __init__(self,messages=None,pending=None):
        super().__init__(messages); self.pending=pending or []; self.claims=[]; self.reads=0
        self.retries={}; self.expiries=[]; self.dead_letters=[]; self.deleted=[]
    async def xautoclaim(self,stream,group,consumer,idle,start_id,**kwargs):
        self.claims.append((stream,group,consumer,idle,start_id)); messages,self.pending=self.pending,[]
        return ("0-0",messages)
    async def xreadgroup(self,g,c,streams,**k):
        self.reads+=1; messages,self.messages=self.messages,[]
        return [(next(iter(streams)),messages)] if messages else []
    async def hincrby(self,key,field,amount):
        self.retries[key]=self.retries.get(key,0)+amount; return self.retries[key]
    async def expire(self,key,ttl): self.expiries.append((key,ttl)); return True
    async def delete(self,key): self.deleted.append(key); self.retries.pop(key,None); return 1
    async def xadd(self,stream,fields,**kwargs): self.dead_letters.append((stream,fields,kwargs)); return "9-0"
class State:
    message_stream="messages"
    namespace="{tenant}"
    def __init__(self): self.issued=[]
    async def issue_selection(self,c,ttl=600): self.issued.append((c,ttl)); return "tok"
class WeCom:
    def __init__(self): self.sent=[]
    async def send_text(self,u,c): self.sent.append((u,c)); return {}
def run(c): return asyncio.run(c)
def candidate(item="id1"): return Candidate(source_id="src",source_version="1",item_id=item,title="Song",artist="Artist")

def test_message_decoder_reads_real_bytes_payload_envelope():
    fields={b"payload":b'{"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"song","msg_type":"text"}}'}
    assert _envelope(fields)["command"]=="search" and _envelope(fields)["value"]=="song" and _envelope(fields)["from_user"]=="u"
def test_message_search_binds_and_sends(monkeypatch):
    async def search(*a,**k): return SearchResult((candidate(),),(),"v1")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); redis,state,wc=Redis(),State(),WeCom()
    token=run(MessageWorker(redis,object(),wc,state=state).handle({"corp_id":"c","from_user":"u","request_id":"r","command":"search","value":"song"}))
    assert token=="tok" and state.issued[0][0].candidates=={1:"id1"}
    assert wc.sent[0][0] == "u" and wc.sent[0][1].startswith("1. Song") and "\n\n" in wc.sent[0][1]
def test_message_empty_result_sends_without_binding(monkeypatch):
    async def search(*a,**k): return SearchResult((),(),"v")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); state,wc=State(),WeCom()
    assert run(MessageWorker(Redis(),object(),wc,state=state).handle({"corp_id":"c","from_user":"u","request_id":"r","command":"search","value":"x"})) is None and state.issued==[] and wc.sent==[("u","没有找到匹配结果。")]
def test_message_ai_failure_falls_back(monkeypatch):
    async def search(*a,**k): return SearchResult((candidate(),),(),"v")
    async def rank(*a): raise RuntimeError("AI down")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); state=State()
    run(MessageWorker(Redis(),object(),WeCom(),state=state,ai_ranker=rank).handle({"corp_id":"c","from_user":"u","request_id":"r","command":"search","value":"x"}))
    assert state.issued[0][0].candidates=={1:"id1"}
def test_message_ai_ranker_receives_search_and_query_and_airank_result_is_used(monkeypatch):
    async def search(*a,**k): return SearchResult((candidate("first"),),(),"v")
    calls=[]
    async def rank(result, query):
        calls.append((result, query))
        return AIRankResult(SearchResult((candidate("ranked"),),(),"rv"), True)
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); state=State()
    run(MessageWorker(Redis(),object(),WeCom(),state=state,ai_ranker=rank).handle({"corp_id":"c","from_user":"u","request_id":"r","command":"search","value":"x"}))
    assert calls[0][1]=="x" and state.issued[0][0].candidates=={1:"ranked"}

def test_message_uses_real_nested_command_envelope(monkeypatch):
    searches=[]
    async def search(_registry, query, **_kwargs):
        searches.append(query); return SearchResult((candidate(),),(),"v")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search)
    envelope={"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"real","msg_type":"text","content":"wrong"}}
    run(MessageWorker(Redis(),object(),WeCom(),state=State()).handle(envelope))
    assert searches==["real"]

def test_message_non_search_command_is_ignored(monkeypatch):
    async def unexpected(*_a,**_k): raise AssertionError("search must not run")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",unexpected)
    assert run(MessageWorker(Redis(),object(),WeCom(),state=State()).handle({"command":"select","value":2})) is None

def test_message_selection_namespace_ttl_and_prompt_fit_byte_limit(monkeypatch):
    async def search(*_a,**_k):
        long=candidate().model_copy(update={"title":"歌"*3000})
        return SearchResult((long,),(),"v")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search)
    redis,state,wc=Redis(),State(),WeCom()
    run(MessageWorker(redis,object(),wc,state=state,selection_ttl=321).handle({"corp_id":"c","from_user":"u","request_id":"r","command":"search","value":"x"}))
    assert state.issued[0][1]==321
    assert all(key.startswith("{tenant}:") for key in redis.values)
    assert len(wc.sent[0][1].encode("utf-8")) <= 2048
def test_message_bad_payload_is_acked():
    redis=Redis([("1-0",{b"payload":b"not-json"})]); assert run(MessageWorker(redis,object(),WeCom(),state=State()).run_once())==0 and redis.acks==[("messages","musicdl-workers","1-0")]
@pytest.mark.parametrize("raw", [b"[]",b"null",b'{"payload":[]}',b'{"payload":null}'])
def test_message_non_mapping_envelopes_are_bad_messages_and_acked(raw):
    redis=StreamRedis([("1-0",{b"payload":raw})])
    assert run(MessageWorker(redis,object(),WeCom(),state=State()).run_once())==0 and redis.acks[-1][2]=="1-0"
def test_message_business_failure_is_not_acked(monkeypatch):
    async def fail(*a,**k): raise RuntimeError("failed")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",fail); raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"content":"x"}}).encode(); redis=Redis([("1-0",{b"payload":raw})])
    assert run(MessageWorker(redis,object(),WeCom(),state=State()).run_once())==0 and redis.acks==[]
def test_message_group_busy_error_is_idempotent():
    class Busy(Redis):
        async def xgroup_create(self,*a,**k): raise RuntimeError("BUSYGROUP exists")
    assert run(MessageWorker(Busy(),object(),WeCom(),state=State()).run_once())==0

def test_message_reclaims_stale_pending_before_new_messages(monkeypatch):
    async def search(*_a,**_k): return SearchResult((candidate(),),(),"v")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search)
    pending=[("1-0",{b"payload":json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"song"}}).encode()})]
    redis=StreamRedis(messages=[("2-0",{})],pending=pending)
    assert run(MessageWorker(redis,object(),WeCom(),state=State(),pending_idle_ms=1234).run_once())==1
    assert redis.claims[0][3]==1234 and redis.reads==0 and redis.acks[0][2]=="1-0"

def test_message_failure_retries_then_dead_letters_notifies_and_acks(monkeypatch):
    async def fail(*_a,**_k): raise RuntimeError("secret failure detail")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",fail)
    raw=json.dumps({"corp_id":"c","from_user":"user-1","request_id":"r","payload":{"command":"search","value":"private query"}}).encode()
    message=("1-0",{b"payload":raw}); redis=StreamRedis(messages=[message]); wc=WeCom()
    worker=MessageWorker(redis,object(),wc,state=State(),max_attempts=2)
    assert run(worker.run_once())==0 and redis.acks==[] and list(redis.retries.values())==[1]
    redis.pending=[message]
    assert run(worker.run_once())==0 and redis.acks[-1][2]=="1-0"
    stream,fields,_=redis.dead_letters[0]
    assert stream=="{tenant}:stream:dead-letter" and fields["reason"]=="business_failure"
    assert "private query" not in json.dumps(fields) and "secret failure detail" not in json.dumps(fields)
    assert wc.sent==[("user-1","处理失败，请稍后重试。")]
    assert redis.retries=={} and redis.deleted

def test_message_success_clears_prior_retry_state(monkeypatch):
    outcomes=[RuntimeError("down"),SearchResult((candidate(),),(),"v")]
    async def search(*_a,**_k):
        value=outcomes.pop(0)
        if isinstance(value,Exception): raise value
        return value
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"song"}}).encode()
    message=("1-0",{b"payload":raw}); redis=StreamRedis(messages=[message]); worker=MessageWorker(redis,object(),WeCom(),state=State())
    run(worker.run_once()); retry_key=next(iter(redis.retries)); redis.pending=[message]
    assert run(worker.run_once())==1 and retry_key in redis.deleted and retry_key not in redis.retries

def test_message_retry_bookkeeping_failure_never_acks(monkeypatch):
    async def fail(*_a,**_k): raise RuntimeError("business failure")
    class Broken(StreamRedis):
        async def hincrby(self,*_a,**_k): raise RuntimeError("redis unavailable")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",fail)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"song"}}).encode()
    redis=Broken(messages=[("1-0",{b"payload":raw})])
    assert run(MessageWorker(redis,object(),WeCom(),state=State()).run_once())==0 and redis.acks==[]

@pytest.mark.parametrize("failure", ["dlq","ack"])
def test_message_terminal_delivery_failure_keeps_retry_key(monkeypatch,failure):
    async def fail(*_a,**_k): raise RuntimeError("business failure")
    class Broken(StreamRedis):
        async def xadd(self,*args,**kwargs):
            if failure=="dlq": raise RuntimeError("dlq unavailable")
            return await super().xadd(*args,**kwargs)
        async def xack(self,*args,**kwargs):
            if failure=="ack": raise RuntimeError("ack unavailable")
            return await super().xack(*args,**kwargs)
    monkeypatch.setattr("musicdl.worker.workers.search_sources",fail)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"song"}}).encode()
    redis=Broken(messages=[("1-0",{b"payload":raw})])
    run(MessageWorker(redis,object(),WeCom(),state=State(),max_attempts=1).run_once())
    assert list(redis.retries.values())==[1] and redis.deleted==[]

@pytest.mark.parametrize("failure", ["ack","delete"])
def test_message_success_acks_before_best_effort_retry_cleanup(monkeypatch,failure):
    calls=[]
    async def search(*_a,**_k): calls.append(1); return SearchResult((candidate(),),(),"v")
    class Broken(StreamRedis):
        async def xack(self,*args,**kwargs):
            if failure=="ack": raise RuntimeError("ack unavailable")
            return await super().xack(*args,**kwargs)
        async def delete(self,*args,**kwargs):
            if failure=="delete": raise RuntimeError("delete unavailable")
            return await super().delete(*args,**kwargs)
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search)
    raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"command":"search","value":"song"}}).encode()
    message=("1-0",{b"payload":raw}); redis=Broken(messages=[message]); worker=MessageWorker(redis,object(),WeCom(),state=State(),max_attempts=1)
    key=worker._retry_key(worker.stream,"1-0"); redis.retries[key]=1
    run(worker.run_once())
    assert key in redis.retries and redis.dead_letters==[] and calls==[1]
    assert (redis.acks==[]) if failure=="ack" else (redis.acks[-1][2]=="1-0")

def test_message_malformed_ack_failure_does_not_clear_retry():
    class Broken(StreamRedis):
        async def xack(self,*_a,**_k): raise RuntimeError("ack unavailable")
    redis=Broken(messages=[("1-0",{b"payload":b"not-json"})]); worker=MessageWorker(redis,object(),WeCom(),state=State())
    key=worker._retry_key(worker.stream,"1-0"); redis.retries[key]=1
    run(worker.run_once())
    assert key in redis.retries and redis.deleted==[]

@pytest.mark.parametrize("kwargs", [
    {"pending_idle_ms":0}, {"pending_idle_ms":True}, {"pending_idle_ms":604800001},
    {"max_attempts":0}, {"max_attempts":True}, {"max_attempts":101},
])
def test_message_delivery_settings_are_strictly_validated(kwargs):
    with pytest.raises(ValueError):
        MessageWorker(Redis(),object(),WeCom(),state=State(),**kwargs)

def test_message_run_forever_propagates_cancellation():
    class Idle(MessageWorker):
        async def run_once(self): return 0
    async def scenario():
        task=asyncio.create_task(Idle(Redis(),object(),WeCom(),state=State()).run_forever(poll_interval=0.01))
        await asyncio.sleep(0); task.cancel()
        try: await task
        except asyncio.CancelledError: return True
        return False
    assert run(scenario()) is True

@pytest.mark.parametrize("interval", [float("nan"),float("inf"),float("-inf")])
def test_message_run_forever_rejects_non_finite_poll_interval(interval):
    with pytest.raises(ValueError,match="poll interval"):
        run(MessageWorker(Redis(),object(),WeCom(),state=State()).run_forever(poll_interval=interval))
