"""Shared cross-process provider pacing; never retries a transaction submission."""
import os
import math
import ipaddress
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime
from data_policy import require_permitted_url,SourceAccessBlocked


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
        db=sqlite3.connect(self.path,timeout=30)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS providers(host TEXT PRIMARY KEY,next_at REAL,blocked REAL,attempts INTEGER,denied INTEGER)')
            db.execute("CREATE TABLE IF NOT EXISTS provider_bursts(host TEXT PRIMARY KEY,count INTEGER NOT NULL,ready_at REAL NOT NULL)")
            return db
        except BaseException:
            db.close()
            raise

    def reserve(self,url,interval=0,*,burst_requests=None,burst_cooldown_seconds=10):
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
                burst_cooldown_seconds=max(10,burst_cooldown_seconds)
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
        finally:db.close()
        if at>now:self.sleep(at-now)

    def reserve_rpc(self,url,interval=0):
        """Default external RPC burst cap, including wallet/live RPC clients."""
        host=(urlparse(url).hostname or '').lower()
        local=host=='localhost'
        try:
            address=ipaddress.ip_address(host)
            local=address.is_loopback or address.is_private
        except ValueError:pass
        self.reserve(url,interval,burst_requests=None if local else 10)

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
                retry=(headers or {}).get('Retry-After')
                if retry:
                    try:wait=max(wait,float(retry))
                    except ValueError:
                        try:wait=max(wait,parsedate_to_datetime(retry).timestamp()-self.clock())
                        except (ValueError,TypeError,OverflowError):wait=max(wait,60)
                db.execute('INSERT OR REPLACE INTO providers VALUES(?,?,?,?,?)',
                    (host,row[0],max(row[1],self.clock()+wait),attempts,int(status in (401,403) or row[3])))
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
