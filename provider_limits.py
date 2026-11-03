"""Shared cross-process provider pacing; never retries a transaction submission."""
import os
import math
import ipaddress
import hashlib
import hmac
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime
from data_policy import require_permitted_url,SourceAccessBlocked

THROTTLE_MARGIN_SECONDS=60


class ProviderBackoff(ValueError):
    def __init__(self,seconds):
        self.seconds=seconds
        super().__init__('Provider cooldown active')


def provider_limits_path(index_db=None):
    """Runtime data belongs outside the package; reuse existing pacing history."""
    configured=os.environ.get('CRYPTO_BOT_PROVIDER_LIMITS')
    if configured:return Path(configured).resolve()
    legacy=Path.cwd()/'state/provider_limits.sqlite'
    if index_db is None:return legacy
    selected=Path(index_db).resolve().parent/'provider_limits.sqlite'
    if selected.exists():return selected
    return legacy if legacy.exists() else selected


class ProviderBudget:
    def __init__(self,path=None,clock=None,sleep=None):
        self.path=Path(path).resolve() if path is not None else provider_limits_path()
        self.clock=clock or time.time;self.sleep=sleep or time.sleep

    def key(self,url):
        host=(urlparse(url).hostname or '').lower()
        if not host:raise ValueError('Provider hostname required')
        return 'solana-public-rpc' if host in ('api.mainnet-beta.solana.com','api.mainnet.solana.com') else host

    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        except FileExistsError:pass
        db=sqlite3.connect(self.path,timeout=30)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS providers(host TEXT PRIMARY KEY,next_at REAL,blocked REAL,attempts INTEGER,denied INTEGER)')
            db.execute("CREATE TABLE IF NOT EXISTS provider_bursts(host TEXT PRIMARY KEY,count INTEGER NOT NULL,ready_at REAL NOT NULL)")
            db.execute('CREATE TABLE IF NOT EXISTS provider_audit_key(id INTEGER PRIMARY KEY CHECK(id=1),secret BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS provider_events(id INTEGER PRIMARY KEY,at REAL NOT NULL,provider_id TEXT NOT NULL,endpoint_id TEXT NOT NULL,pid INTEGER NOT NULL,consumer TEXT NOT NULL,event TEXT NOT NULL,method TEXT,logical_calls INTEGER,http_status INTEGER,retry_after_seconds REAL,blocked_until REAL,origin TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS provider_events_time ON provider_events(at)')
            return db
        except BaseException:
            db.close()
            raise

    def _identity(self,db,url):
        db.execute('INSERT OR IGNORE INTO provider_audit_key VALUES(1,?)',(os.urandom(32),))
        secret=db.execute('SELECT secret FROM provider_audit_key WHERE id=1').fetchone()[0]
        def digest(text):return hmac.new(secret,text.encode(),hashlib.sha256).hexdigest()
        return digest(self.key(url)),digest(url)

    def identity(self,url):
        db=self.connect()
        try:
            with db:return dict(zip(('provider_id','endpoint_id'),self._identity(db,url)))
        finally:db.close()

    def _audit(self,db,url,event,method=None,logical_calls=1,status=None,retry=None,blocked=None,origin=None):
        # No raw URLs, headers, parameters, wallet IDs or credentials enter the log.
        allowed={'getGenesisHash','getSlot','getBlocks','getBlock','getAccountInfo','getBlockTime',
                 'getSignaturesForAddress','getTransaction','getFirstAvailableBlock','minimumLedgerSlot',
                 'getHealth','getVersion','sendTransaction','getBalance','getLatestBlockhash',
                 'getSignatureStatuses','simulateTransaction','getTokenAccountsByOwner'}
        method=method if method in allowed else None
        consumer=os.environ.get('CRYPTO_BOT_RPC_CONSUMER','standalone')
        if consumer not in ('indexer','learning','consumer','live','probe-python','probe-rust','standalone'):consumer='other'
        provider,endpoint=self._identity(db,url)
        db.execute('INSERT INTO provider_events(at,provider_id,endpoint_id,pid,consumer,event,method,logical_calls,http_status,retry_after_seconds,blocked_until,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (self.clock(),provider,endpoint,os.getpid(),consumer,event,method,logical_calls,status,retry,blocked,origin))

    def audit_attempt(self,url,method,logical_calls=1):
        db=self.connect()
        try:
            with db:self._audit(db,url,'client_attempt',method,logical_calls,origin='local')
        finally:db.close()

    def audit_http(self,url,method,status):
        db=self.connect()
        try:
            with db:self._audit(db,url,'http_response',method,status=status,origin='http')
        finally:db.close()

    def audit_wait(self,url,method,seconds):
        db=self.connect()
        try:
            with db:self._audit(db,url,'wait',method,blocked=self.clock()+seconds,origin='local')
        finally:db.close()

    def reserve(self,url,interval=0,*,burst_requests=None,burst_cooldown_seconds=30,rpc_method=None,logical_calls=1):
        require_permitted_url(url)
        host=self.key(url);minimum=6. if host=='api.geckoterminal.com' else .5
        if burst_requests is None and host=='solana-public-rpc':burst_requests=10
        if burst_requests is not None:
            if type(burst_requests) is not int or not 1<=burst_requests<=100:
                raise ValueError('RPC burst requests must be 1..100')
            if isinstance(burst_cooldown_seconds,bool) or not isinstance(burst_cooldown_seconds,(int,float)) or not math.isfinite(burst_cooldown_seconds) or not 1<=burst_cooldown_seconds<=3600:
                raise ValueError('RPC burst cooldown must be 1..3600 seconds')
            if host=='solana-public-rpc':
                burst_requests=min(10,burst_requests)
                burst_cooldown_seconds=max(30,burst_cooldown_seconds)
        db=self.connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT next_at,blocked,attempts,denied FROM providers WHERE host=?',(host,)).fetchone() or (0,0,0,0)
                now=self.clock()
                if row[3]:raise SourceAccessBlocked('Provider access denied; manual authorization review required')
                if row[1]>now:raise ProviderBackoff(row[1]-now)
                burst=db.execute('SELECT count,ready_at FROM provider_bursts WHERE host=?',(host,)).fetchone()
                if burst and burst[1]>now:raise ProviderBackoff(burst[1]-now)
                at=max(now,row[0])
                if burst_requests is not None:
                    count=(burst[0] if burst and not burst[1] else 0)+1
                    ready=at+max(minimum,interval,burst_cooldown_seconds) if count>=burst_requests else 0
                    db.execute('INSERT OR REPLACE INTO provider_bursts VALUES(?,?,?)',(host,count,ready))
                db.execute('INSERT OR REPLACE INTO providers VALUES(?,?,?,?,?)',(host,at+max(minimum,interval),row[1],row[2],0))
                self._audit(db,url,'reservation',rpc_method,logical_calls,origin='local')
        finally:db.close()
        if at>now:
            self.sleep(at-now)
            # A different process may have recorded Retry-After during our wait.
            # The reservation stays consumed: it is never replayed or refunded.
            db=self.connect()
            try:
                row=db.execute('SELECT blocked,denied FROM providers WHERE host=?',(host,)).fetchone()
                if row and row[1]:raise SourceAccessBlocked('Provider access denied; manual authorization review required')
                if row and row[0]>self.clock():raise ProviderBackoff(row[0]-self.clock())
            finally:db.close()

    def reserve_rpc(self,url,interval=0,method=None):
        """Default external RPC burst cap, including wallet/live RPC clients."""
        host=(urlparse(url).hostname or '').lower().rstrip('.')
        local=host=='localhost'
        try:
            address=ipaddress.ip_address(host)
            local=address.is_loopback or address.is_private
        except ValueError:pass
        self.reserve(url,interval,burst_requests=None if local else 10,rpc_method=method)
        self.audit_attempt(url,method)

    def failure(self,url,status,headers=None):
        if status not in (401,403,429,500,502,503,504):return
        db=self.connect();host=self.key(url)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT next_at,blocked,attempts,denied FROM providers WHERE host=?',(host,)).fetchone() or (0,0,0,0)
                attempts=row[2]+1;wait=min(600,2**min(attempts,9))
                if status==429:wait=max(wait,60)
                if status==503:wait=max(wait,5)
                server_retry=None
                retry=(headers or {}).get('Retry-After')
                if retry:
                    try:server_retry=max(0,float(retry));wait=max(wait,server_retry)
                    except ValueError:
                        try:server_retry=max(0,parsedate_to_datetime(retry).timestamp()-self.clock());wait=max(wait,server_retry)
                        except (ValueError,TypeError,OverflowError):wait=max(wait,60)
                if status==429:wait+=THROTTLE_MARGIN_SECONDS
                deadline=max(row[1],self.clock()+wait)
                db.execute('INSERT OR REPLACE INTO providers VALUES(?,?,?,?,?)',
                    (host,row[0],deadline,attempts,int(status in (401,403) or row[3])))
            # Diagnostic failure must never roll back an already received throttle/denial.
            with db:
                self._audit(db,url,'backoff',status=status if headers is not None else None,retry=server_retry,
                            blocked=deadline,origin='http' if headers is not None else 'internal_or_rpc')
        finally:db.close()

    def cooldown_remaining(self,url):
        """Observe the shared deadline without clearing a denial or cooldown."""
        db=self.connect()
        try:
            host=self.key(url)
            row=db.execute('SELECT blocked FROM providers WHERE host=?',(host,)).fetchone()
            burst=db.execute('SELECT ready_at FROM provider_bursts WHERE host=?',(host,)).fetchone()
            return max(0,(row[0] if row else 0)-self.clock(),(burst[0] if burst else 0)-self.clock())
        finally:db.close()

    def success(self,url):
        db=self.connect()
        try:
            with db:db.execute('UPDATE providers SET attempts=0 WHERE host=?',(self.key(url),))
        finally:db.close()


BUDGET=ProviderBudget()
