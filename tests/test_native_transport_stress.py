"""Opt-in LOOPBACK ONLY native socket tests, no external RPC.

Default discovery skips these. Run on the monolith, not alongside production
traffic. Creates only ephemeral local servers; no namespace/firewall changes.
"""
import asyncio
import json
import os
import sys
import unittest
try:
    from _solana_binary import NativeHttp, connect_ws
except ImportError:
    NativeHttp=connect_ws=None

ENABLED=os.environ.get('SOLANA_NATIVE_LOOPBACK_STRESS')=='1' and sys.platform=='linux' and NativeHttp is not None and hasattr(NativeHttp,'request_sync') and hasattr(NativeHttp,'close')


@unittest.skipUnless(ENABLED,'Explicit monolith loopback stress and native rpc-engine required')
class NativeTransportStress(unittest.IsolatedAsyncioTestCase):
    async def test_python_and_native_bounded_blocks_preserve_ids_and_pacing(self):
        import gzip
        from unittest.mock import MagicMock,patch
        from rpc_loopback_fixture import LocalRpcServer
        from onchain_ingest import IndexRpc
        from solana_rpc_compat import RustIndexRpc
        inode=os.stat('/proc/self/ns/net').st_ino
        rows=[{'jsonrpc':'2.0','id':2,'result':{'transactions':[],'blockhash':'second'}},
              {'jsonrpc':'2.0','id':1,'result':{'transactions':[],'blockhash':'first'}}]
        for backend in ('python','rust'):
            for encoding in ('identity','gzip'):
                for chunked in (False,True):
                    with self.subTest(backend=backend,encoding=encoding,chunked=chunked):
                        with LocalRpcServer() as server:
                            body=json.dumps(rows).encode()
                            server.script=[{'body':gzip.compress(body) if encoding=='gzip' else body,
                                            'encoding':encoding,'chunked':chunked}]
                            budget=MagicMock()
                            rpc=(RustIndexRpc if backend=='rust' else IndexRpc)(
                                {'url':server.url,'source_id':'fixture','allow_loopback_http':True,
                                 'block_batch_size':2,'request_interval_seconds':.5},budget)
                            try:
                                with patch('solana_rpc_compat.check_boundary',return_value=inode):
                                    result=await asyncio.to_thread(rpc.call_blocks,[5,6],{'commitment':'finalized'})
                                self.assertEqual([item['blockhash'] for item in result],['first','second'])
                                budget.reserve.assert_called_once_with(server.url,.5)
                                self.assertEqual(rpc.requests,1)
                                calls=server.requests[0]
                                self.assertEqual([call['id'] for call in calls],[1,2])
                                self.assertTrue(all(call['method']=='getBlock' for call in calls))
                            finally:
                                if backend=='rust':rpc.close()

    async def test_recorded_event_bytes_survive_transport_indexer_and_storage(self):
        import copy
        import gzip
        import hashlib
        from pathlib import Path
        import tempfile
        import zlib
        from unittest.mock import MagicMock,patch
        from rpc_loopback_fixture import LocalRpcServer
        from onchain_decode import Decoder
        from onchain_ingest import IndexRpc,Ingester
        from onchain_store import EventStore,canonical
        from solana_rpc_compat import RustIndexRpc,extract_event_batch
        from wallet_watch import MAINNET
        root=Path(__file__).parent/'fixtures/onchain'
        manifest=json.loads((root/'manifest.json').read_text())
        inode=os.stat('/proc/self/ns/net').st_ino
        policy={'storage_verified':True,'evidence':'Recorded fixture loopback regression only'}
        for fixture in manifest['fixtures']:
            packed=(root/fixture['file']).read_bytes()
            self.assertEqual(hashlib.sha256(packed).hexdigest(),fixture['sha256'])
            original=json.loads(packed)
            slot=fixture['slot']
            block={'blockTime':original.get('blockTime',fixture.get('block_time')),'blockhash':'fixture-block',
                   'previousBlockhash':'fixture-parent','parentSlot':slot-1,
                   'transactions':[original]}
            before=canonical(original)
            direct=Decoder('compare').transaction(original,slot,0,block['blockTime'])
            self.assertNotIn('Unreviewed event layout/version',direct[1])
            for backend in ('python','rust'):
                for encoding in ('identity','gzip'):
                    for chunked in (False,True):
                        with self.subTest(fixture=fixture['file'],backend=backend,encoding=encoding,chunked=chunked):
                            with LocalRpcServer() as server,tempfile.TemporaryDirectory() as directory:
                                for result in (MAINNET,slot,slot,[slot],slot,block):
                                    body=json.dumps({'jsonrpc':'2.0','id':1,'result':result}).encode()
                                    server.script.append({'body':gzip.compress(body) if encoding=='gzip' else body,
                                                          'encoding':encoding,'chunked':chunked})
                                endpoint={'url':server.url,'allow_loopback_http':True,'source_id':'fixture'}
                                client=(RustIndexRpc if backend=='rust' else IndexRpc)(endpoint,MagicMock())
                                # Mock deployment probes, not transport or block/transaction decoding.
                                # This test checks byte preservation, not source/deployment admission.
                                store=EventStore(Path(directory)/'index.sqlite',retain_blocks=True)
                                store.register_source('fixture',policy)
                                decoder=Decoder('compare')
                                deployments={program:1 for program in decoder.specs}
                                versions={program:[{'from_slot':slot,'through_slot':slot,
                                    'deployment_slot':1,'idl_hash':spec['hash']}]
                                    for program,spec in decoder.specs.items()}
                                ingester=Ingester(store,[client],versions=versions,decoder=decoder)
                                try:
                                    with patch('solana_rpc_compat.check_boundary',return_value=inode),patch.object(ingester,'deployments',return_value=deployments):
                                        await asyncio.to_thread(ingester.step,start=slot,count=1)
                                finally:
                                    if backend=='rust':client.close()
                                with store.connect() as db:
                                    saved=db.execute('SELECT raw,raw_hash FROM transactions').fetchone()
                                    saved_block=db.execute('SELECT raw FROM raw_blocks').fetchone()[0]
                                    self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],len(direct[0]))
                                restored=json.loads(zlib.decompress(saved[0]))
                                self.assertEqual(canonical(restored),before)
                                self.assertEqual(saved[1],hashlib.sha256(before.encode()).hexdigest())
                                restored_block=json.loads(zlib.decompress(saved_block))
                                restored_block.pop('_indexer_deployments')
                                self.assertEqual(restored_block,block)
                                self.assertEqual(Decoder('compare').transaction(restored,slot,0,block['blockTime']),direct)
                                projection=extract_event_batch([copy.deepcopy(restored)],slot=slot,
                                    block_time=block['blockTime'],source_id='fixture',backend='compare')
                                self.assertEqual(bool(projection['issues']),bool(direct[1]))
                                self.assertEqual(len(projection['rows']),0 if direct[1] else len(direct[0]))
                                self.assertFalse(projection['market_eligible'])
                                self.assertEqual(canonical(original),before)
                                self.assertEqual(store.next_slot(),slot+1)
                                self.assertEqual(len(server.requests),6)

    @unittest.skipUnless(NativeHttp is not None and hasattr(NativeHttp,'request_encoded_sync'),'Build native gzip transport methods')
    async def test_gzip_wire_transport_and_strict_decompression(self):
        import gzip
        from unittest.mock import MagicMock,patch
        from rpc_loopback_fixture import LocalRpcServer
        from solana_rpc_compat import RustIndexRpc
        from data_policy import SourceAccessBlocked
        raw=(json.dumps({'jsonrpc':'2.0','id':1,'result':123})+' '*4096).encode()
        compressed=gzip.compress(raw)
        with LocalRpcServer() as server:
            server.script=[{'body':compressed,'encoding':'gzip'},
                           {'body':compressed+b'junk','encoding':'gzip'}]
            inode=os.stat('/proc/self/ns/net').st_ino
            client=NativeHttp(server.url,inode,True)
            rpc=RustIndexRpc({'url':server.url,'allow_loopback_http':True,'source_id':'fixture'},MagicMock())
            rpc._transport=client
            try:
                with patch('solana_rpc_compat.check_boundary',return_value=inode):
                    self.assertEqual(await rpc.call_async('getSlot',[]),123)
                    self.assertEqual(rpc.wire_bytes,len(compressed))
                    self.assertEqual(rpc.decoded_bytes,len(raw))
                    self.assertLess(rpc.wire_bytes,rpc.decoded_bytes)
                    with self.assertRaises(SourceAccessBlocked):await rpc.call_async('getSlot',[])
                self.assertEqual(len(server.requests),2)
            finally:client.close()

    async def test_http_format_errors_backpressure_and_cancellation(self):
        release=asyncio.Event();entered=asyncio.Event();requests=[];handlers=set()
        async def serve(reader,writer):
            task=asyncio.current_task();handlers.add(task)
            try:
                header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),2)
                headers=dict(line.split(b':',1) for line in header.split(b'\r\n')[1:] if b':' in line)
                size=int(next(value for key,value in headers.items() if key.lower()==b'content-length'))
                self.assertLessEqual(size,65536)
                request=json.loads(await reader.readexactly(size));requests.append(request)
                entered.set();await release.wait()
                body=json.dumps({'jsonrpc':'2.0','id':1,'result':123}).encode()
                status=request['params'][0] if request['params'] and request['params'][0] in (429,503) else 200
                writer.write(b'HTTP/1.1 '+str(status).encode()+b' Fixture\r\nRetry-After: 60\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
                await writer.drain()
            except (ConnectionError,asyncio.IncompleteReadError):
                pass # expected when a client cancels its response
            finally:
                writer.close()
                try: await writer.wait_closed()
                except ConnectionError: pass
                finally: handlers.discard(task)
        server=await asyncio.start_server(serve,'127.0.0.1',0,limit=65536)
        client=None
        try:
            client=NativeHttp('http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]),os.stat('/proc/self/ns/net').st_ino,True)
            pending=asyncio.ensure_future(client.request('getSlot','[]'))
            await asyncio.wait_for(entered.wait(),2)
            with self.assertRaises(RuntimeError):client.request('getSlot','[]')
            release.set()
            status,_,text=await asyncio.wait_for(pending,3)
            self.assertEqual((status,json.loads(text)['result']),(200,123))
            self.assertEqual(requests[0],{'jsonrpc':'2.0','id':1,'method':'getSlot','params':[]})
            for status in (429,503):
                observed,retry,_=await asyncio.wait_for(client.request('getSlot',json.dumps([status])),3)
                self.assertEqual((observed,retry),(status,'60'))
            # Cancel a blocked body response; the per-client permit must recover.
            release.clear();entered.clear()
            pending=asyncio.ensure_future(client.request('getSlot','[]'))
            await asyncio.wait_for(entered.wait(),2);pending.cancel()
            with self.assertRaises(asyncio.CancelledError):await pending
            release.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                try: next_request=client.request('getSlot','[]');break
                except RuntimeError:continue
            else:self.fail('Cancelled HTTP operation did not release its permit')
            await asyncio.wait_for(next_request,3)
            release.clear();entered.clear()
            pending=asyncio.ensure_future(client.request('getSlot','[]'))
            await asyncio.wait_for(entered.wait(),2)
            client.close()
            with self.assertRaises(ConnectionError):await asyncio.wait_for(pending,3)
            with self.assertRaises(ConnectionError):client.request('getSlot','[]')
        finally:
            if client is not None:client.close()
            release.set();server.close();await server.wait_closed()
            if handlers:await asyncio.wait_for(asyncio.gather(*list(handlers),return_exceptions=True),3)

    async def test_ws_simultaneous_send_receive_close_and_bursts(self):
        from websockets.asyncio.server import serve
        gate=asyncio.Event();connections=[]
        async def handler(ws):
            connections.append(ws)
            await ws.recv() # send must work while native recv is already pending
            for index in range(32):await ws.send(json.dumps({'sequence':index}))
            await gate.wait()
        async with serve(handler,'127.0.0.1',0,max_queue=4,max_size=2000000) as server:
            endpoint='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
            try:
                for _ in range(3): # explicit bounded reconnection, no implicit fallback
                    ws=await asyncio.wait_for(connect_ws(endpoint,os.stat('/proc/self/ns/net').st_ino,True),3)
                    first=asyncio.ensure_future(ws.recv())
                    await ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'logsSubscribe','params':[]}))
                    self.assertEqual(json.loads(await asyncio.wait_for(first,3))['sequence'],0)
                    for index in range(1,32):self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(),3))['sequence'],index)
                    pending=asyncio.ensure_future(ws.recv())
                    await asyncio.wait_for(ws.close(),3)
                    with self.assertRaises(ConnectionError):await asyncio.wait_for(pending,3)
                self.assertEqual(len(connections),3)
            finally:
                gate.set()

    async def test_quiet_stream_survives_old_thirty_second_timeout(self):
        from websockets.asyncio.server import serve
        async def handler(ws):
            await ws.recv()
            await asyncio.sleep(31) # deliberately crosses the former idle deadline
            await ws.send('quiet-stream-alive')
        async with serve(handler,'127.0.0.1',0,ping_interval=None,close_timeout=2) as server:
            endpoint='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
            ws=await asyncio.wait_for(connect_ws(endpoint,os.stat('/proc/self/ns/net').st_ino,True),3)
            try:
                await ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'logsSubscribe','params':[]}))
                self.assertEqual(await asyncio.wait_for(ws.recv(),36),'quiet-stream-alive')
            finally:await asyncio.wait_for(ws.close(),3)
