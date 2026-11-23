"""Loopback RPC for locally committed, hash-verified finalized block evidence."""
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

from onchain_store import EventStore
from sync_policy import slot


class RpcError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def dispatch(store, method, params):
    if method == 'freya_getStatus' and params == []:
        with store.connect() as db:
            count, first, last = db.execute(
                "SELECT count(*), min(b.slot), max(b.slot) FROM blocks b "
                "JOIN raw_blocks r ON r.slot = b.slot WHERE b.issues = '[]'"
            ).fetchone()
        return {'mode': 'retained_finalized_blocks', 'retained_blocks': count,
                'first_slot': first, 'last_slot': last, 'live_rpc_ready': False}
    if method != 'getBlock':
        raise RpcError(-32601, 'Method is unavailable from retained block evidence')
    if not isinstance(params, list) or len(params) not in (1, 2):
        raise RpcError(-32602, 'Expected slot and optional finalized JSON options')
    try:
        requested = slot(params[0])
    except ValueError as error:
        raise RpcError(-32602, 'Invalid slot') from error
    options = params[1] if len(params) == 2 else {}
    supported = {'commitment', 'encoding', 'transactionDetails', 'rewards', 'maxSupportedTransactionVersion'}
    if (not isinstance(options, dict) or set(options) - supported
            or options.get('commitment', 'finalized') != 'finalized'
            or options.get('encoding', 'json') != 'json'
            or options.get('transactionDetails', 'full') != 'full'
            or 'rewards' in options and options['rewards'] is not False
            or type(options.get('maxSupportedTransactionVersion', 0)) is not int
            or options.get('maxSupportedTransactionVersion', 0) not in (0, 1)):
        raise RpcError(-32602, 'Only finalized full JSON requests are supported')
    with store.connect() as db:
        header = db.execute('SELECT hash,parent,time,issues FROM blocks WHERE slot=?', (requested,)).fetchone()
    if header is None or header['issues'] != '[]':
        raise RpcError(-32004, 'Finalized block evidence unavailable or quarantined')
    raw = store.retained_block(requested)
    if raw is None:
        raise RpcError(-32004, 'Full finalized block evidence was not retained')
    block = json.loads(raw)
    if (block['blockhash'], block['parentSlot'], block['blockTime']) != (
            header['hash'], header['parent'], header['time']):
        raise RpcError(-32004, 'Retained block header conflict')
    maximum = options.get('maxSupportedTransactionVersion', 0)
    for transaction in block['transactions']:
        version = transaction.get('version', 'legacy')
        if version != 'legacy' and (type(version) is not int or not 0 <= version <= maximum):
            raise RpcError(-32015, 'Unsupported retained transaction version')
        if not isinstance(transaction.get('meta'), dict):
            raise RpcError(-32004, 'Retained execution receipt unavailable')
    result = {key: value for key, value in block.items() if not key.startswith('_')}
    # Retained upstream requests did not necessarily include rewards.
    result.pop('rewards', None)
    return result


def serve(database, port):
    store = EventStore(database, readonly=True)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def do_POST(self):
            request_id = None
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 8192 or self.headers.get('Transfer-Encoding'):
                    raise RpcError(-32600, 'Invalid bounded request length')
                try:
                    request = json.loads(self.rfile.read(size))
                except (UnicodeError, ValueError, RecursionError) as error:
                    raise RpcError(-32700, 'Invalid JSON') from error
                if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                        or type(request.get('id')) not in (str, int)
                        or not isinstance(request.get('method'), str)):
                    raise RpcError(-32600, 'Invalid JSON-RPC request')
                request_id = request['id']
                result = dispatch(store, request['method'], request.get('params', []))
                body = json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}, allow_nan=False).encode()
                if len(body) > 16_000_000:
                    raise RpcError(-32004, 'Retained response exceeds the RPC size limit')
            except RpcError as error:
                body = self.error_body(request_id, error.code, str(error))
            except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
                body = self.error_body(request_id, -32004, 'Local finalized evidence unavailable')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def error_body(request_id, code, message):
            return json.dumps({'jsonrpc': '2.0', 'id': request_id,
                               'error': {'code': code, 'message': message}}).encode()

        def log_message(self, *_args):
            pass

    with HTTPServer(('127.0.0.1', port), Handler) as server:
        server.serve_forever()
