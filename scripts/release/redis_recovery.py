"""Opt-in isolated Redis recovery probe; never flushes or scans a database."""
import argparse, secrets, sys
def main():
 p=argparse.ArgumentParser(); p.add_argument('--url',required=True); p.add_argument('--namespace',default='musicdl-gate-'+secrets.token_hex(6)); p.add_argument('--confirm-isolated',action='store_true'); a=p.parse_args()
 if not a.confirm_isolated: print('[redis-recovery] FAIL - --confirm-isolated is required'); return 2
 if not a.namespace.startswith('musicdl-gate-'): print('[redis-recovery] FAIL - namespace must be isolated musicdl-gate-*'); return 2
 try:
  import redis
  r=redis.Redis.from_url(a.url, socket_connect_timeout=5, socket_timeout=5); key=a.namespace+':canary'; stream=a.namespace+':stream'
  try:
   r.set(key,'ok',ex=120); r.xadd(stream,{'status':'canary'},maxlen=10); r.expire(stream,120); r.connection_pool.disconnect()
   if r.get(key)!=b'ok' or not r.xread({stream:'0-0'},count=1,block=1000): raise RuntimeError('recovery check failed')
   print('[redis-recovery] PASS - isolated canary reconnect/read/consume/cleanup'); return 0
  finally:
   try: r.delete(key,stream)
   except Exception: pass
 except ImportError: print('[redis-recovery] NOT_RUN - redis client unavailable'); return 3
 except Exception: print('[redis-recovery] FAIL - isolated probe failed; details suppressed'); return 1
if __name__=='__main__': sys.exit(main())
