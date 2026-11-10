"""Atomic finalized-block journal. No access to trading/evaluation databases."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib
from sync_policy import initial_slot, slot as historical_slot, GENESIS_SLOT


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class EventStore:
    def __init__(self, path, readonly=False, retain_blocks=False):
        self.path = Path(path)
        self.readonly = readonly
        self.retain_blocks = retain_blocks
        if readonly:
            if not self.path.is_file():
                raise ValueError("Local index database does not exist; start the indexer first")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS blocks(
                    slot INTEGER PRIMARY KEY, hash TEXT NOT NULL, parent INTEGER NOT NULL,
                    time INTEGER, source TEXT NOT NULL, issues TEXT NOT NULL,
                    raw_hash TEXT NOT NULL, transactions INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS block_times ON blocks(time);
                CREATE TABLE IF NOT EXISTS raw_blocks(slot INTEGER PRIMARY KEY, raw BLOB NOT NULL, raw_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS native_reconciliations(
                    slot INTEGER PRIMARY KEY, source TEXT NOT NULL, evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transactions(
                    signature TEXT PRIMARY KEY, slot INTEGER NOT NULL, tx_index INTEGER NOT NULL,
                    raw BLOB NOT NULL, raw_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    signature TEXT NOT NULL, outer_index INTEGER NOT NULL, event_index INTEGER NOT NULL,
                    slot INTEGER NOT NULL, tx_index INTEGER NOT NULL, time INTEGER NOT NULL,
                    mint TEXT, pool TEXT, wallet TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(signature,outer_index,event_index));
                CREATE INDEX IF NOT EXISTS market_events ON events(mint,pool,time);
                CREATE INDEX IF NOT EXISTS wallet_events ON events(wallet,time);
                CREATE TABLE IF NOT EXISTS ranges(
                    start INTEGER PRIMARY KEY, end INTEGER NOT NULL, source TEXT NOT NULL,
                    slots TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS range_ends ON ranges(end);
                CREATE TABLE IF NOT EXISTS telemetry(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history_requests(
                    source TEXT PRIMARY KEY, cursor INTEGER NOT NULL, scan_next INTEGER NOT NULL,
                    requested_end INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversions(
                    mint TEXT NOT NULL, time INTEGER NOT NULL, usd TEXT NOT NULL,
                    source TEXT NOT NULL, proof TEXT NOT NULL, PRIMARY KEY(mint,time,source));
            ''')
            # Historical RPC blockTime may legitimately be null. Preserve every
            # existing row/index/trigger in one transaction when relaxing this
            # constraint; unknown times remain NULL, never synthetic timestamps.
            columns = db.execute('PRAGMA table_info(blocks)').fetchall()
            if next(row for row in columns if row['name']=='time')['notnull']:
                if [row['name'] for row in columns]!=['slot','hash','parent','time','source','issues','raw_hash','transactions']:
                    raise ValueError('Unknown historical block schema; explicit migration required')
                objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='blocks' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE blocks_nullable(slot INTEGER PRIMARY KEY, hash TEXT NOT NULL, parent INTEGER NOT NULL, time INTEGER, source TEXT NOT NULL, issues TEXT NOT NULL, raw_hash TEXT NOT NULL, transactions INTEGER NOT NULL)')
                db.execute('INSERT INTO blocks_nullable SELECT slot,hash,parent,time,source,issues,raw_hash,transactions FROM blocks')
                db.execute('DROP TABLE blocks')
                db.execute('ALTER TABLE blocks_nullable RENAME TO blocks')
                for row in objects:
                    db.execute(row[0])

    @contextmanager
    def connect(self):
        if self.readonly:
            from urllib.parse import quote
            db = sqlite3.connect('file:' + quote(str(self.path.resolve()), safe='/') + '?mode=ro', uri=True, timeout=30)
            try:
                db.execute('PRAGMA query_only=ON')
            except BaseException:
                db.close()
                raise
        else:
            db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if not self.readonly:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def register_source(self, identifier, policy):
        if not policy.get('storage_verified') or not policy.get('evidence'):
            raise ValueError('Verified storage policy/evidence required')
        with self.connect() as db:
            row = db.execute('SELECT policy FROM sources WHERE id=?', (identifier,)).fetchone()
            if row and row[0] != canonical(policy):
                raise ValueError('Source policy immutable; use a new versioned source ID')
            db.execute('INSERT OR IGNORE INTO sources VALUES(?,?)', (identifier, canonical(policy)))

    def policy(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT policy FROM sources WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Unregistered source')
        return json.loads(row[0])

    def retained_block(self, slot):
        """Verified raw evidence from hot storage or an explicitly indexed archive."""
        from onchain_hour import unpack_block
        with self.connect() as db:
            header = db.execute('SELECT raw_hash FROM blocks WHERE slot=?', (slot,)).fetchone()
            row = db.execute('SELECT raw,raw_hash FROM raw_blocks WHERE slot=?', (slot,)).fetchone()
            if row:
                if not header or row['raw_hash'] != header['raw_hash']:
                    raise ValueError('Retained metadata hash mismatch')
                try:
                    raw = unpack_block(row['raw'])
                except zlib.error as error:
                    raise ValueError('Corrupt retained block payload') from error
            else:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='retention_archives'").fetchone()
                archive = db.execute('SELECT * FROM retention_archives WHERE slot=?', (slot,)).fetchone() if exists else None
                if archive is None:
                    return None
                from observer_archive import verify_archive
                raw, manifest = verify_archive(archive['path'])
                if manifest['slot'] != slot or manifest['raw_sha256'] != archive['raw_hash']:
                    raise ValueError('Archive catalog mismatch')
            if not header or hashlib.sha256(raw).hexdigest() != header['raw_hash']:
                raise ValueError('Retained evidence hash mismatch')
            return raw

    def next_slot(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM checkpoints WHERE name='next_slot'").fetchone()
        return initial_slot(row[0]) if row else None

    def history_request(self, source, cursor, scan_next=None, end=None, state='requested', reason=''):
        """Uncommitted discovery state, separate from the durable prefix."""
        with self.connect() as db:
            if scan_next is None:
                row = db.execute('SELECT cursor,scan_next,state FROM history_requests WHERE source=?', (source,)).fetchone()
                if row and row['cursor']==cursor and row['state'] in ('unresolved','blocked'):
                    historical_slot(row['scan_next'])
                    if row['scan_next']<cursor:
                        raise ValueError('Historical discovery cursor precedes committed progress')
                    return row['scan_next']
                return cursor
            historical_slot(cursor)
            historical_slot(scan_next)
            db.execute('INSERT OR REPLACE INTO history_requests VALUES(?,?,?,?,?,?)',
                       (source,cursor,scan_next,end,state,reason))

    def history_status(self):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='history_requests'").fetchone():
                return []  # Read-only status of a pre-migration database.
            return [dict(row) for row in db.execute('SELECT * FROM history_requests ORDER BY source')]

    def commit_range(self, start, end, slots, decoded, source):
        """Persist manifest, all produced blocks and checkpoint in one transaction.

        getBlocks supplies skipped-slot evidence; RPC failures never count as skips.
        decoded: [(slot, full block, events, issues, relevant raw transactions)].
        """
        self.policy(source)
        historical_slot(start)
        historical_slot(end)
        if end < start or slots != sorted(set(slots)) or any(type(s) is not int for s in slots):
            raise ValueError('Invalid finalized slot manifest')
        if any(not start <= slot <= end for slot in slots) or [x[0] for x in decoded] != slots:
            raise ValueError('Incomplete manifest blocks')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            checkpoint = db.execute("SELECT value FROM checkpoints WHERE name='next_slot'").fetchone()
            existing = db.execute('SELECT end,source,slots FROM ranges WHERE start=?', (start,)).fetchone()
            if existing:
                if tuple(existing) != (end, source, canonical(slots)):
                    raise ValueError('Conflicting finalized manifest')
                # Still verify every supplied block below; never overwrite history.
            elif not slots or slots[-1]!=end:
                raise ValueError('Unanchored trailing omissions are unresolved history, not committed skips')
            elif checkpoint and checkpoint[0] != start:
                raise ValueError('Noncontiguous ingestion; recover checkpoint first')
            previous = db.execute('SELECT slot,time,hash FROM blocks WHERE slot<? ORDER BY slot DESC LIMIT 1', (start,)).fetchone()
            known_time = db.execute('SELECT max(time) FROM blocks WHERE slot<?', (start,)).fetchone()[0]
            for slot, block, events, issues, transactions in decoded:
                stamp = block['blockTime']
                if (stamp is not None and (type(stamp) is not int or stamp<0)) or not block.get('blockhash'):
                    raise ValueError('Finalized block time/hash unavailable')
                if stamp is not None and known_time is not None and stamp<known_time:
                    raise ValueError('Finalized time continuity mismatch across unknown timestamps')
                parent=block['parentSlot']
                if type(parent) is not int or parent<GENESIS_SLOT or parent>=slot:
                    raise ValueError('Invalid finalized parent slot')
                if previous and (parent != previous[0] or stamp is not None and previous[1] is not None and stamp < previous[1]
                                 or block['previousBlockhash'] != previous[2]):
                    raise ValueError('Finalized parent/time continuity mismatch')
                if previous is None and parent>=max(start,GENESIS_SLOT+1):
                    raise ValueError('Unresolved history before first retrieved block')
                digest = hashlib.sha256(canonical(block).encode()).hexdigest()
                row = db.execute('SELECT raw_hash FROM blocks WHERE slot=?', (slot,)).fetchone()
                if row and row[0] != digest:
                    raise ValueError('Conflicting finalized block')
                if '_native_reconciliation' in block:
                    evidence = canonical(block['_native_reconciliation'])
                    old = db.execute('SELECT source,evidence FROM native_reconciliations WHERE slot=?',
                                     (slot,)).fetchone()
                    if old and tuple(old) != (source, evidence):
                        raise ValueError('Conflicting native reconciliation provenance')
                    db.execute('INSERT OR IGNORE INTO native_reconciliations VALUES(?,?,?)',
                               (slot, source, evidence))
                if self.retain_blocks:
                    db.execute('INSERT OR IGNORE INTO raw_blocks VALUES(?,?,?)',
                               (slot, zlib.compress(canonical(block).encode()), digest))
                db.execute('INSERT OR IGNORE INTO blocks VALUES(?,?,?,?,?,?,?,?)',
                           (slot, block['blockhash'], block['parentSlot'], stamp, source,
                            canonical(issues), digest, len(block['transactions'])))
                for index, tx in transactions:
                    raw = canonical(tx).encode()
                    signature = tx['transaction']['signatures'][0]
                    old = db.execute('SELECT raw_hash FROM transactions WHERE signature=?', (signature,)).fetchone()
                    raw_hash = hashlib.sha256(raw).hexdigest()
                    if old and old[0] != raw_hash:
                        raise ValueError('Conflicting transaction provenance')
                    db.execute('INSERT OR IGNORE INTO transactions VALUES(?,?,?,?,?)',
                               (signature, slot, index, zlib.compress(raw), raw_hash))
                for event in events:
                    payload = canonical(event)
                    key = (event['signature'], event['outer_index'], event['event_index'])
                    old = db.execute('SELECT payload FROM events WHERE signature=? AND outer_index=? AND event_index=?', key).fetchone()
                    if old and old[0] != payload:
                        raise ValueError('Conflicting canonical event')
                    db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (*key, slot, event['tx_index'], stamp, event.get('mint'),
                                event.get('pool'), event.get('wallet'), event['kind'], payload))
                previous = (slot, stamp, block['blockhash'])
                if stamp is not None:
                    known_time=stamp
            db.execute('INSERT OR IGNORE INTO ranges VALUES(?,?,?,?)',
                       (start, end, source, canonical(slots)))
            if not existing:
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES('next_slot',?)", (end + 1,))

    def coverage(self, start, end):
        if end <= start:
            raise ValueError('Invalid coverage interval')
        with self.connect() as db:
            left = db.execute('SELECT slot FROM blocks WHERE time<=? ORDER BY time DESC,slot ASC LIMIT 1', (start,)).fetchone()
            right = db.execute('SELECT slot,time FROM blocks WHERE time>=? ORDER BY time,slot LIMIT 1', (end,)).fetchone()
            if not left or not right:
                return {'complete': False, 'coverage_pct': 0, 'reason': 'window not bracketed by finalized blocks'}
            blocks = db.execute('SELECT * FROM blocks WHERE slot BETWEEN ? AND ? ORDER BY slot', (left[0], right[0])).fetchall()
            reasons = sorted({reason for b in blocks for reason in json.loads(b['issues'])})
            sources = sorted({b['source'] for b in blocks})
            # Ranges are contiguous by construction; detect any damaged manifest too.
            ranges = db.execute('SELECT start,end FROM ranges WHERE end>=? AND start<=? ORDER BY start', (left[0], right[0])).fetchall()
            cursor = left[0]
            for row in ranges:
                if row[0] > cursor:
                    reasons.append('missing finalized slot range')
                cursor = max(cursor, row[1] + 1)
            if cursor <= right[0]:
                reasons.append('missing finalized slot range')
        return {'complete': not reasons, 'coverage_pct': 0 if reasons else 100,
                'reason': '; '.join(reasons) if reasons else 'continuous finalized block manifests',
                'sources': sources, 'through_time': right[1],
                'first_slot': left[0], 'last_slot': right[0]}

    def events(self, mint=None, pool=None, start=0, end=2**63-1, wallet=None, verified_only=False):
        query = 'SELECT payload FROM events WHERE time>=? AND time<?'
        args = [start, end]
        if verified_only:
            query += " AND slot IN (SELECT slot FROM blocks WHERE issues='[]')"
        for key, value in (('mint', mint), ('pool', pool), ('wallet', wallet)):
            if value is not None:
                query += ' AND ' + key + '=?'
                args.append(value)
        query += ' ORDER BY slot,tx_index,outer_index,event_index'
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, args)]

    def add_conversion(self, mint, timestamp, usd, source, proof):
        from decimal import Decimal
        price = Decimal(str(usd))
        if not price.is_finite() or price <= 0 or type(timestamp) is not int or not proof:
            raise ValueError('Invalid event-time conversion/proof')
        policy = self.policy(source)
        if policy.get('usd_conversion_verified') is not True:
            raise ValueError('USD conversion method/rights have not been reviewed')
        with self.connect() as db:
            old = db.execute('SELECT usd,proof FROM conversions WHERE mint=? AND time=? AND source=?', (mint, timestamp, source)).fetchone()
            if old and tuple(old) != (str(price), canonical(proof)):
                raise ValueError('Immutable conversion conflict')
            db.execute('INSERT OR IGNORE INTO conversions VALUES(?,?,?,?,?)',
                       (mint, timestamp, str(price), source, canonical(proof)))

    def conversion(self, mint, timestamp, max_age=60, max_slot=None):
        from decimal import Decimal
        with self.connect() as db:
            rows = db.execute('SELECT * FROM conversions WHERE mint=? AND time<=? AND time>=? ORDER BY time DESC',
                              (mint, timestamp, timestamp-max_age)).fetchall()
        if max_slot is not None:
            rows = [row for row in rows if json.loads(row['proof']).get('posted_slot', 0) <= max_slot]
        if not rows:
            raise ValueError('Fresh causal historical USD conversion unavailable')
        newest = [row for row in rows if row['time'] == rows[0]['time']]
        if len({Decimal(row['usd']) for row in newest}) != 1:
            raise ValueError('Conflicting USD conversion sources')
        return Decimal(rows[0]['usd']), rows[0]['source'], rows[0]['time']

    def save_progress(self, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO telemetry VALUES(?,?)', ('progress', canonical(value)))

    def progress(self):
        with self.connect() as db:
            # Older index databases remain readable until the writer migrates them.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='telemetry'").fetchone():
                return None
            row = db.execute("SELECT payload FROM telemetry WHERE name='progress'").fetchone()
        return json.loads(row[0]) if row else None

    def watermark(self):
        with self.connect() as db:
            return db.execute('SELECT max(time) FROM blocks').fetchone()[0]

    def metrics(self):
        with self.connect() as db:
            result = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('blocks', 'events', 'transactions', 'ranges', 'conversions')}
            result['blocks_with_issues'] = db.execute("SELECT count(*) FROM blocks WHERE issues!='[]'").fetchone()[0]
            result['oldest_time'], result['latest_time'] = db.execute('SELECT min(time),max(time) FROM blocks').fetchone()
            result['committed_next_slot'] = db.execute('SELECT max(r.end)+1 FROM ranges r JOIN blocks b ON b.slot=r.end').fetchone()[0]
        result['next_slot'] = self.next_slot()
        result['history_requests'] = self.history_status()
        return result
