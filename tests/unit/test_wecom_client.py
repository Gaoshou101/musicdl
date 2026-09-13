import asyncio, json, httpx, pytest
from musicdl.wecom.client import WeComClient, WeComError
class Redis:
    def __init__(self): self.values={}; self.calls=[]
    async def get(self,k): return self.values.get(k)
    async def set(self,k,v,**kw): self.values[k]=v; self.calls.append((k,v,kw))
def run(c): return asyncio.run(c)
def test_token_is_cached_and_message_uses_cached_token():
    r=Redis()
    def handler(req):
        if req.url.path.endswith("/gettoken"): return httpx.Response(200,json={"errcode":0,"access_token":"tok","expires_in":100})
        assert req.url.params["access_token"]=="tok" and json.loads(req.content)["touser"]=="u1"; return httpx.Response(200,json={"errcode":0,"msgid":"m"})
    c=WeComClient("corp","secret",7,r,base_url="https://x",transport=httpx.MockTransport(handler)); assert run(c.send_text("u1","hello"))["msgid"]=="m"; assert run(c.access_token())=="tok"; assert r.calls[0][2]["ex"]==40
def test_token_error_is_stable_and_redacted():
    def h(req): return httpx.Response(200,json={"errcode":40013,"errmsg":"secret-body"})
    with pytest.raises(WeComError,match="token request failed") as e: run(WeComClient("c","s",1,Redis(),base_url="https://x",transport=httpx.MockTransport(h)).access_token())
    assert "secret-body" not in str(e.value)
def test_send_error_is_stable_and_redacted():
    r=Redis(); r.values["{musicdl}:wecom:access_token"]="tok"
    def h(req): return httpx.Response(200,json={"errcode":93000,"errmsg":"private-response"})
    with pytest.raises(WeComError,match="message send failed") as e: run(WeComClient("c","s",1,r,base_url="https://x",transport=httpx.MockTransport(h)).send_text("u","x"))
    assert "private-response" not in str(e.value)
def test_expired_token_refreshes_once_and_reuses_new_token():
    r=Redis(); calls=[]
    def h(req):
        calls.append(req); return httpx.Response(200,json={"errcode":40014} if req.url.path.endswith("/message/send") and req.url.params["access_token"]=="old" else ({"errcode":0,"access_token":"new","expires_in":120} if req.url.path.endswith("/gettoken") else {"errcode":0}))
    r.values["{musicdl}:wecom:access_token"]="old"; c=WeComClient("c","s",1,r,base_url="https://x",transport=httpx.MockTransport(h)); assert run(c.send_text("u","x"))["errcode"]==0; assert [x.url.path for x in calls]==["/cgi-bin/message/send","/cgi-bin/gettoken","/cgi-bin/message/send"]
def test_second_expired_response_stops_after_two_sends():
    r=Redis(); r.values["{musicdl}:wecom:access_token"]="old"; calls=[]
    def h(req): calls.append(req); return httpx.Response(200,json={"errcode":40014} if req.url.path.endswith("send") else {"errcode":0,"access_token":"new","expires_in":100})
    with pytest.raises(WeComError): run(WeComClient("c","s",1,r,base_url="https://x",transport=httpx.MockTransport(h)).send_text("u","x"))
    assert len([x for x in calls if x.url.path.endswith("send")])==2
@pytest.mark.parametrize("expired_code", [40014,42001])
def test_expired_token_codes_refresh_once(expired_code):
    r=Redis(); r.values["{musicdl}:wecom:access_token"]="old"; calls=[]
    def h(req):
        calls.append(req)
        if req.url.path.endswith("gettoken"): return httpx.Response(200,json={"errcode":0,"access_token":"new","expires_in":100})
        return httpx.Response(200,json={"errcode":expired_code if req.url.params["access_token"]=="old" else 0})
    assert run(WeComClient("c","s",1,r,base_url="https://x",transport=httpx.MockTransport(h)).send_text("u","x"))["errcode"]==0
    assert [x.url.path for x in calls]==["/cgi-bin/message/send","/cgi-bin/gettoken","/cgi-bin/message/send"]

def test_concurrent_cache_miss_fetches_token_once():
    r=Redis(); calls=[]
    async def h(req):
        calls.append(req); await asyncio.sleep(0.01)
        return httpx.Response(200,json={"errcode":0,"access_token":"new","expires_in":100})
    client=WeComClient("c","s",1,r,base_url="https://x",transport=httpx.MockTransport(h))
    async def scenario(): return await asyncio.gather(client.access_token(),client.access_token(),client.access_token())
    assert run(scenario())==["new","new","new"] and len(calls)==1

@pytest.mark.parametrize("operation", ["get","set"])
def test_redis_failures_are_stable_and_redacted(operation):
    class Broken(Redis):
        async def get(self,k):
            if operation=="get": raise RuntimeError("redis-secret")
            return None
        async def set(self,k,v,**kw):
            if operation=="set": raise RuntimeError("redis-secret")
            return await super().set(k,v,**kw)
    def h(req): return httpx.Response(200,json={"errcode":0,"access_token":"new","expires_in":100})
    with pytest.raises(WeComError,match="token request failed") as error:
        run(WeComClient("c","s",1,Broken(),base_url="https://x",transport=httpx.MockTransport(h)).access_token())
    assert "redis-secret" not in str(error.value)

@pytest.mark.parametrize("endpoint", ["token","send"])
@pytest.mark.parametrize("body", [b"not-json", b"[]"])
def test_malformed_or_non_object_json_is_stable(endpoint,body):
    r=Redis()
    if endpoint=="send": r.values["{musicdl}:wecom:access_token"]="tok"
    def h(req): return httpx.Response(200,content=body,headers={"content-type":"application/json"})
    call=WeComClient("c","s",1,r,base_url="https://x",transport=httpx.MockTransport(h))
    expected="token request failed" if endpoint=="token" else "message send failed"
    with pytest.raises(WeComError,match=expected): run(call.access_token() if endpoint=="token" else call.send_text("u","x"))

def test_message_limit_is_utf8_bytes():
    c=WeComClient("c","s",1,Redis())
    with pytest.raises(ValueError): run(c.send_text("u","歌"*683))
    # 682 CJK code points are 2046 UTF-8 bytes and remain valid.
    c.redis.values[c.cache_key]="tok"
    c.transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"errcode":0}))
    assert run(c.send_text("u","歌"*682))["errcode"]==0
def test_invalid_message_inputs_are_rejected():
    c=WeComClient("c","s",1,Redis())
    for user,content in [("","x"),("u","") ,("u","x"*2049)]:
        with pytest.raises(ValueError): run(c.send_text(user,content))

def test_owned_http_client_disables_proxy_environment(monkeypatch):
    seen = []
    class SpyClient:
        def __init__(self, **kwargs): seen.append(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def request(self, *_args, **_kwargs):
            class Response:
                def raise_for_status(self): pass
                def json(self): return {"errcode": 0, "access_token": "tok", "expires_in": 120}
            return Response()
    monkeypatch.setattr("musicdl.wecom.client.httpx.AsyncClient", SpyClient)
    run(WeComClient("c", "s", 1, Redis()).access_token())
    assert seen and seen[0]["trust_env"] is False
