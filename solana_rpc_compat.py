"""Experimental Rust transport behind established Python ingestion contracts.

Default/auto select Python. No signing, networking outside the approved boundary,
backend fallback after evidence errors, native program decoding or native storage.
"""
import asyncio
import json
import threading
from data_policy import SourceAccessBlocked, require_permitted_url
from onchain_ingest import IndexRpc, READ_METHODS
from wallet_watch import PublicRpcError
from rpc_contract import response_value, remote_error, http_error, rpc_result, rpc_batch_result


def native_module():
    try:
        import _solana_binary as native
        if not hasattr(native,'NativeHttp'): raise ImportError
        return native
    except ImportError:
        raise RuntimeError('Build the optional rpc-engine extension') from None


def check_boundary(endpoint):
    import os
    from solana_network_budget import Budget
    policy=endpoint.get('policy',{})
    if policy.get('storage_verified') is not True or not policy.get('evidence'):
        raise SourceAccessBlocked('Reviewed source storage policy required')
    mode=endpoint.get('network_mode','isolated')
    if mode not in ('isolated','remote_provider'):
        raise SourceAccessBlocked('Unknown RPC network mode')
    if mode=='remote_provider':
        import ipaddress
        from urllib.parse import urlsplit
        parsed=urlsplit(endpoint['url']);host=parsed.hostname
        if (endpoint.get('source_kind')!='solana_rpc' or policy.get('remote_provider_verified') is not True
            or endpoint.get('network_budget') or parsed.scheme!='https' or not host or parsed.username or parsed.password
            or host.lower().rstrip('.')=='localhost' or host.lower().rstrip('.').endswith(('.localhost','.local'))):
            raise SourceAccessBlocked('Explicit reviewed remote HTTPS provider without a node budget required')
        try:address=ipaddress.ip_address(host)
        except ValueError:address=None
        if address is not None and not address.is_global:
            raise SourceAccessBlocked('Local/private nodes cannot use remote-provider mode')
        ws=endpoint.get('ws_url')
        if ws:
            websocket=urlsplit(ws)
            if websocket.scheme!='wss' or websocket.hostname!=host or websocket.username or websocket.password:
                raise SourceAccessBlocked('Remote WebSocket must use the same reviewed TLS provider')
        require_permitted_url(endpoint['url'])
        inode=os.stat('/proc/self/ns/net').st_ino
        if endpoint.get('approved_namespace_inode',inode)!=inode:
            raise SourceAccessBlocked('Configured namespace mismatch')
        return inode
    import time
    from pathlib import Path
    path=Path(endpoint.get('network_budget',{}).get('status_file','/run/solana-budget-status.json'))
    try:
        stat=path.stat()
        if path.is_symlink() or stat.st_uid!=0 or stat.st_mode & 0o022 or not 0<=time.time()-stat.st_mtime<=5:
            raise ValueError('Untrusted/stale session status')
        with path.open('rb') as stream: raw=stream.read(4097)
        if len(raw)>4096: raise ValueError('Oversized session status')
        status=json.loads(raw)
        inode=status['namespace_inode']
        if type(inode) is not int or inode<=0 or os.stat('/proc/self/ns/net').st_ino!=inode:
            raise ValueError('Namespace mismatch')
        if type(status['expires']) not in (int,float) or not __import__('math').isfinite(status['expires']) or not time.time()<status['expires']<=time.time()+60:
            raise ValueError('Expired session')
        if status['optional_allowed'] is not True: raise ValueError('Normal allocation exhausted')
        if endpoint.get('approved_namespace_inode',inode)!=inode: raise ValueError('Configured namespace mismatch')
    except Exception:
        raise SourceAccessBlocked('Network session unavailable or unauthorized; no RPC connection') from None
    require_permitted_url(endpoint['url'])
    return inode



class RustIndexRpc(IndexRpc):
    def __init__(self,endpoint,budget=None):
        super().__init__(endpoint,budget)
        self.endpoint=endpoint
        self._transport=None
        self._transport_lock=threading.Lock()
        self._closed=False

    def _transport_for(self,inode):
        with self._transport_lock:
            if self._closed: raise RuntimeError('RPC client is closed')
            if self._transport is None:
                self._transport=native_module().NativeHttp(self.url,inode,self.endpoint.get('allow_loopback_http',False))
            return self._transport

    def _parameters(self,method,params):
        if self._closed: raise RuntimeError('RPC client is closed')
        if method not in READ_METHODS: raise SourceAccessBlocked('Read-only RPC methods only')
        if not isinstance(params,list): raise SourceAccessBlocked('RPC params must be an array')
        try: text=json.dumps(params,allow_nan=False)
        except (TypeError,ValueError,RecursionError): raise SourceAccessBlocked('Invalid RPC request parameters') from None
        if len(text.encode())>65536: raise SourceAccessBlocked('RPC request exceeds bound')
        return text

    def _transport_error(self,error):
        if self._closed: raise RuntimeError('RPC client closed during request') from None
        if isinstance(error,ConnectionError):
            self.budget.failure(self.url,503)
            raise error
        raise SourceAccessBlocked('Malformed native transport evidence; no backend retry') from None

    async def call_async(self,method,params):
        text=self._parameters(method,params)
        inode=check_boundary(self.endpoint)
        await asyncio.to_thread(self._reserve,method)
        transport=self._transport_for(inode)
        self.requests+=1
        encoded=getattr(type(transport),'request_encoded',None) is not None
        try: response=await (transport.request_encoded(method,text) if encoded else transport.request(method,text))
        except (ConnectionError,ValueError) as error: self._transport_error(error)
        return (self._finish_encoded if encoded else self._finish)(*response,method=method)

    def call(self,method,params):
        try: asyncio.get_running_loop()
        except RuntimeError: pass
        else: raise RuntimeError('Use await call_async() inside an asyncio loop')
        text=self._parameters(method,params)
        inode=check_boundary(self.endpoint)
        self._reserve(method)
        transport=self._transport_for(inode)
        if not hasattr(transport,'request_sync'): raise RuntimeError('Rebuild native RPC extension with request_sync')
        self.requests+=1
        encoded=getattr(type(transport),'request_encoded_sync',None) is not None
        try: response=transport.request_encoded_sync(method,text) if encoded else transport.request_sync(method,text)
        except (ConnectionError,ValueError) as error: self._transport_error(error)
        return (self._finish_encoded if encoded else self._finish)(*response,method=method)

    def close(self):
        with self._transport_lock:
            self._closed=True
            if self._transport is not None: self._transport.close()

    def call_blocks(self,slots,options):
        if not isinstance(slots,list) or not 1<=len(slots)<=self.block_batch_size or any(type(slot) is not int or not 0<=slot<2**63 for slot in slots) or len(set(slots))!=len(slots):
            raise ValueError('Invalid bounded block request')
        if len(slots)==1:return [self.call('getBlock',[slots[0],options])]
        try:asyncio.get_running_loop()
        except RuntimeError:pass
        else:raise RuntimeError('Run synchronous block batching outside the asyncio loop')
        params=self._parameters('getBlock',[[slot,options] for slot in slots])
        transport=self._transport_for(check_boundary(self.endpoint))
        if not hasattr(type(transport),'request_blocks_encoded_sync'):
            raise RuntimeError('Rebuild native RPC extension with bounded block batching')
        self._reserve('getBlock')
        self.requests+=1
        try:response=transport.request_blocks_encoded_sync(params)
        except (ConnectionError,ValueError) as error:self._transport_error(error)
        return self._finish_encoded(*response,method='getBlock',batch_count=len(slots))

    async def aclose(self): self.close()
    def __enter__(self): return self
    def __exit__(self,*_): self.close()

    def _finish(self,status,retry,text,method=None):
        if status!=200:
            self.budget.failure(self.url,status,{'Retry-After':retry} if retry else {})
            failure=http_error(status,self.source,method)
            if getattr(type(self.budget),'cooldown_remaining',None) is not None:
                failure.retry_after_seconds=self.budget.cooldown_remaining(self.url)
            raise failure
        size=len(text.encode())
        self.wire_bytes+=size; self.decoded_bytes+=size
        return rpc_result(text,method,self.source,self.budget,self.url)

    def _finish_encoded(self,status,retry,encoding,text,method=None,batch_count=None):
        if status!=200:return self._finish(status,retry,'',method=method)
        # Native identity replies are already UTF-8-validated strings. Keep the
        # original text for JSON validation instead of encoding/decoding it twice.
        if encoding=='identity':
            try:size=len(text.encode('utf8'))
            except (UnicodeError,AttributeError):
                raise SourceAccessBlocked('Malformed native identity evidence; no backend retry') from None
            if size>64_000_000:
                raise SourceAccessBlocked('Oversized native identity evidence; no backend retry')
            self.wire_bytes+=size;self.decoded_bytes+=size
            if batch_count is not None:return rpc_batch_result(text,self.source,self.budget,self.url,batch_count)
            return rpc_result(text,method,self.source,self.budget,self.url)
        import base64
        from onchain_ingest import decode_rpc_body
        try:
            if encoding=='gzip':
                if len(text)>85_333_336:raise ValueError('Oversized compressed response')
                raw=base64.b64decode(text,validate=True)
            else:raise ValueError('Unsupported response encoding')
            decoded=decode_rpc_body(raw,encoding)
            body=decoded.decode('utf8')
        except (ValueError,UnicodeError):
            raise SourceAccessBlocked('Malformed compressed native evidence; no backend retry') from None
        self.wire_bytes+=len(raw);self.decoded_bytes+=len(decoded)
        if batch_count is not None:return rpc_batch_result(body,self.source,self.budget,self.url,batch_count)
        return rpc_result(body,method,self.source,self.budget,self.url)

    def decode_binary(self,response,*,tx_index,commitment):
        from solana_binary_ingest import ingest_rpc_response
        return ingest_rpc_response(response,source_id=self.source,policy=self.endpoint['policy'],
                                   tx_index=tx_index,commitment=commitment,backend='rust')


def close_rpc_clients(clients):
    """Close every owned client even if one cleanup raises; never resets state."""
    from contextlib import ExitStack
    with ExitStack() as stack:
        for client in clients:
            close=getattr(client,'close',None)
            if callable(close):stack.callback(close)


def make_rpc(endpoint,budget=None):
    backend=endpoint.get('backend','python')
    if backend in ('python','auto'): result=IndexRpc(endpoint,budget)
    elif backend=='rust': result=RustIndexRpc(endpoint,budget)
    else: raise ValueError('Unknown RPC backend')
    result.event_backend=endpoint.get('event_backend','python')
    if result.event_backend not in ('python','rust','compare'): raise ValueError('Unknown event backend')
    return result


def stream_connector(endpoint,python_connector):
    backend=endpoint.get('ws_backend',endpoint.get('backend','python'))
    if backend in ('python','auto'): return python_connector
    if backend!='rust': raise ValueError('Unknown WS backend')
    class Connector:
        def __init__(self,url,**options):
            supported={'max_queue':32,'max_size':2000000,'open_timeout':15,'ping_interval':20,'ping_timeout':20}
            if any(key not in supported or value!=supported[key] for key,value in options.items()):
                raise ValueError('Unsupported native WebSocket option; bounds are explicit and fixed')
            self.url=url; self.socket=None
        async def __aenter__(self):
            if self.url!=endpoint.get('ws_url') or endpoint.get('policy',{}).get('stream_verified') is not True:
                raise SourceAccessBlocked('Explicit authorized WebSocket source required')
            inode=check_boundary(endpoint)
            require_permitted_url(self.url)
            try:
                self.socket=await native_module().connect_ws(self.url,inode,endpoint.get('allow_loopback_ws',False))
            except ValueError as error:
                try:
                    detail=json.loads(str(error)); status=detail.get('ws_http_status')
                    if type(status) is int:
                        from provider_limits import BUDGET
                        BUDGET.failure(self.url,status,{'Retry-After':detail.get('retry_after','')})
                        if status in (500,502,503,504): raise ConnectionError('WebSocket provider unavailable') from None
                except (ValueError,TypeError,AttributeError): pass
                raise SourceAccessBlocked('Native WebSocket handshake denied; review source before retry') from None
            return self
        async def __aexit__(self,*_):
            if self.socket is not None: await asyncio.wait_for(asyncio.shield(self.socket.close()),5)
        async def send(self,text):
            check_boundary(endpoint)
            try: await self.socket.send(text)
            except ValueError: raise SourceAccessBlocked('Invalid native subscription evidence') from None
        async def recv(self):
            check_boundary(endpoint)
            try: return await self.socket.recv()
            except ValueError: raise SourceAccessBlocked('Invalid native stream evidence') from None
    return Connector


def extract_event_batch(transactions,*,slot,block_time,source_id,backend='python'):
    """Compact unverified projection; receipt/version checks remain Ingester-owned."""
    from onchain_decode import Decoder
    if not isinstance(transactions,list) or len(transactions)>64: raise ValueError('Event batch exceeds 64 transactions')
    if type(slot) is not int or slot<0 or type(block_time) is not int or not isinstance(source_id,str) or not source_id:
        raise ValueError('Explicit batch context required')
    decoder=Decoder(backend=backend)
    events=[];issues=[]
    for index,tx in enumerate(transactions):
        try: found,errors=decoder.transaction(tx,slot,index,block_time)
        except (ValueError,KeyError,TypeError,IndexError): found,errors=[],['malformed transaction evidence']
        # A transaction with any ambiguous evidence contributes no compact events.
        if errors: issues.append({'tx_index':index,'errors':errors})
        else: events.extend(found)
    columns=sorted({key for event in events for key in event})
    return {'columns':columns,'rows':[[event.get(key) for key in columns] for event in events],
            'presence':[[i for i,key in enumerate(columns) if key in event] for event in events],
            'issues':issues,'source_id':source_id,'market_eligible':False,'execution_verified':False,
            'requires_existing_ingester_validation':True}
