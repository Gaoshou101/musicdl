import asyncio
import json
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
class State:
    message_stream="messages"
    def __init__(self): self.issued=[]
    async def issue_selection(self,c): self.issued.append(c); return "tok"
class WeCom:
    def __init__(self): self.sent=[]
    async def send_text(self,u,c): self.sent.append((u,c)); return {}
def run(c): return asyncio.run(c)
def candidate(item="id1"): return Candidate(source_id="src",source_version="1",item_id=item,title="Song",artist="Artist")

def test_message_decoder_reads_real_bytes_payload_envelope():
    fields={b"payload":b'{"corp_id":"c","from_user":"u","request_id":"r","payload":{"content":"song"}}'}
    assert _envelope(fields)["content"]=="song" and _envelope(fields)["from_user"]=="u"
def test_message_search_binds_and_sends(monkeypatch):
    async def search(*a,**k): return SearchResult((candidate(),),(),"v1")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); redis,state,wc=Redis(),State(),WeCom()
    token=run(MessageWorker(redis,object(),wc,state=state).handle({"corp_id":"c","from_user":"u","request_id":"r","content":"song"}))
    assert token=="tok" and state.issued[0].candidates=={1:"id1"}
    assert wc.sent[0][0] == "u" and wc.sent[0][1].startswith("1. Song") and "\n\n" in wc.sent[0][1]
def test_message_empty_result_sends_without_binding(monkeypatch):
    async def search(*a,**k): return SearchResult((),(),"v")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); state,wc=State(),WeCom()
    assert run(MessageWorker(Redis(),object(),wc,state=state).handle({"corp_id":"c","from_user":"u","request_id":"r","content":"x"})) is None and state.issued==[] and wc.sent==[("u","没有找到匹配结果。")]
def test_message_ai_failure_falls_back(monkeypatch):
    async def search(*a,**k): return SearchResult((candidate(),),(),"v")
    async def rank(*a): raise RuntimeError("AI down")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); state=State()
    run(MessageWorker(Redis(),object(),WeCom(),state=state,ai_ranker=rank).handle({"corp_id":"c","from_user":"u","request_id":"r","content":"x"}))
    assert state.issued[0].candidates=={1:"id1"}
def test_message_ai_ranker_result_is_used(monkeypatch):
    async def search(*a,**k): return SearchResult((candidate("first"),),(),"v")
    async def rank(*a): return SearchResult((candidate("ranked"),),(),"rv")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",search); state=State()
    run(MessageWorker(Redis(),object(),WeCom(),state=state,ai_ranker=rank).handle({"corp_id":"c","from_user":"u","request_id":"r","content":"x"}))
    assert state.issued[0].candidates=={1:"ranked"}
def test_message_bad_payload_is_acked():
    redis=Redis([("1-0",{b"payload":b"not-json"})]); assert run(MessageWorker(redis,object(),WeCom(),state=State()).run_once())==0 and redis.acks==[("messages","musicdl-workers","1-0")]
def test_message_business_failure_is_not_acked(monkeypatch):
    async def fail(*a,**k): raise RuntimeError("failed")
    monkeypatch.setattr("musicdl.worker.workers.search_sources",fail); raw=json.dumps({"corp_id":"c","from_user":"u","request_id":"r","payload":{"content":"x"}}).encode(); redis=Redis([("1-0",{b"payload":raw})])
    assert run(MessageWorker(redis,object(),WeCom(),state=State()).run_once())==0 and redis.acks==[]
def test_message_group_busy_error_is_idempotent():
    class Busy(Redis):
        async def xgroup_create(self,*a,**k): raise RuntimeError("BUSYGROUP exists")
    assert run(MessageWorker(Busy(),object(),WeCom(),state=State()).run_once())==0
