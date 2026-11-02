"""Endpoint-free transport metadata shared by native and Python RPC clients."""
import socket
import ssl
import time

FIELDS=('phase','elapsed_ms','bytes_received','timeout','connect_error','socket_errno',
        'cancelled','client_closed','pooled_connection_reused','backoff_origin')


def transport_error(cause,started,phase='request_send',status=None,received=0):
    reason=getattr(cause,'reason',cause)
    timeout=isinstance(reason,(TimeoutError,socket.timeout))
    if getattr(reason,'connect_timeout',False) is True:phase='connect'
    elif isinstance(reason,socket.gaierror):phase='dns'
    elif isinstance(reason,ssl.SSLError):phase='tls'
    elif isinstance(reason,ConnectionRefusedError):phase='connect'
    error=ConnectionError('RPC transport failure; endpoint omitted')
    error.phase=phase;error.http_status=status
    error.elapsed_ms=max(0,(time.monotonic()-started)*1000)
    error.bytes_received=received;error.timeout=timeout
    error.connect_error=phase in ('dns','connect','tls')
    error.category=('connect_timeout' if timeout and error.connect_error else 'request_timeout' if timeout else
                    phase if error.connect_error else 'transport')
    code=getattr(reason,'errno',None)
    error.socket_errno=code if type(code) is int else None
    error.cancelled=False;error.client_closed=False;error.pooled_connection_reused=None
    return error


def details(error):
    """Whitelist typed metadata; no raw exception strings, causes or endpoint fields."""
    result={}
    for name in FIELDS:
        value=getattr(error,name,None)
        if name in ('phase','backoff_origin'):
            if value not in ('dns','connect','tls','request_send','response_headers','response_body',
                             'client_state','cancellation','local_transport','provider_http'):value=None
        elif name in ('elapsed_ms','bytes_received'):
            import math
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:value=None
        elif name=='socket_errno':
            if type(value) is not int:value=None
        elif value is not None and type(value) is not bool:value=None
        result[name]=value
    return result
