"""Offline diagnostics tests; no actual sockets, keys, or provider state."""
import json
import socket
import time
import unittest
from http.client import IncompleteRead
from unittest.mock import MagicMock,patch
from urllib.error import URLError
from rpc_diagnostics import transport_error,details
from onchain_ingest import IndexRpc
from solana_rpc_compat import RustIndexRpc

ENDPOINT={'url':'https://example.invalid/SECRET-key','source_id':'fixture'}


class DiagnosticTests(unittest.TestCase):
    def test_safe_connect_error_does_not_render_authenticated_cause(self):
        cause=URLError(ConnectionRefusedError(111,'https://example.invalid/SECRET-key'))
        error=transport_error(cause,time.monotonic())
        self.assertEqual(error.phase,'connect')
        self.assertEqual(error.socket_errno,111)
        self.assertNotIn('SECRET',str(error)+json.dumps(details(error)))
        self.assertIsNone(error.http_status)
        self.assertFalse(error.timeout)

    def test_python_partial_body_preserves_status_method_and_bytes(self):
        rpc=IndexRpc(ENDPOINT,MagicMock())
        with patch('onchain_ingest.build_opener') as factory:
            response=factory.return_value.open.return_value.__enter__.return_value
            response.status=200
            response.read.side_effect=IncompleteRead(b'partial',50)
            with self.assertRaises(ConnectionError) as caught:rpc.call('getBlock',[])
        error=caught.exception
        self.assertEqual((error.phase,error.http_status,error.bytes_received),('response_body',200,7))
        self.assertEqual(error.rpc_method,'getBlock')
        self.assertEqual(error.backoff_origin,'local_transport')

    def test_short_body_without_read_exception_is_transport_failure(self):
        rpc=IndexRpc(ENDPOINT,MagicMock())
        with patch('onchain_ingest.build_opener') as factory:
            response=factory.return_value.open.return_value.__enter__.return_value
            response.status=200;response.headers={'Content-Length':'20'};response.read.return_value=b'partial'
            with self.assertRaises(ConnectionError) as caught:rpc.call('getBlock',[])
        self.assertEqual(caught.exception.http_status,200)
        self.assertEqual(caught.exception.bytes_received,7)

    def test_native_metadata_survives_and_cancel_is_not_provider_failure(self):
        budget=MagicMock();rpc=RustIndexRpc(ENDPOINT,budget)
        error=transport_error(TimeoutError('SECRET'),time.monotonic(),phase='response_body',status=200,received=8)
        with self.assertRaises(ConnectionError):rpc._transport_error(error,'getBlock')
        self.assertEqual(error.category,'request_timeout')
        self.assertEqual(error.http_status,200)
        self.assertEqual(error.rpc_method,'getBlock')
        budget.failure.assert_called_once_with(rpc.url,503)
        budget.reset_mock();error.cancelled=True
        with self.assertRaises(ConnectionError):rpc._transport_error(error,'getBlock')
        budget.failure.assert_not_called()

    def test_http_backoff_is_not_a_synthetic_response(self):
        rpc=RustIndexRpc(ENDPOINT,MagicMock())
        for status in (429,503):
            with self.subTest(status=status),self.assertRaises((ValueError,ConnectionError)) as caught:
                rpc._finish(status,'60','',method='getBlock')
            self.assertEqual(caught.exception.http_status,status)
            self.assertEqual(caught.exception.backoff_origin,'provider_http')

    def test_unsafe_metadata_is_not_serialized(self):
        error=ConnectionError('secret')
        error.phase='https://SECRET';error.socket_errno='SECRET';error.elapsed_ms=float('nan')
        self.assertNotIn('SECRET',json.dumps(details(error)))

    def test_close_contract_retains_already_received_http_status(self):
        rpc=RustIndexRpc(ENDPOINT,MagicMock());rpc._closed=True
        error=transport_error(TimeoutError(),time.monotonic(),'response_body',200,8)
        with self.assertRaises(RuntimeError) as caught:rpc._transport_error(error,'getBlock')
        self.assertEqual(caught.exception.http_status,200)
        self.assertEqual(caught.exception.bytes_received,8)
        self.assertTrue(caught.exception.client_closed)
        rpc.budget.failure.assert_not_called()
