"""Shared historical boundary; no network access or initialization side effects."""
import json
from pathlib import Path

POLICY = json.loads(Path(__file__).with_suffix('.json').read_text())
if POLICY.get('schema') != 'freya-sync-policy-v1':
    raise ValueError('Unsupported synchronization policy')
GENESIS_SLOT = POLICY['fresh_start_slot']
GENESIS_HASH = POLICY['genesis_hash']
if type(GENESIS_SLOT) is not int or GENESIS_SLOT != 0:
    raise ValueError('Genesis synchronization boundary must be zero')


def slot(value):
    if type(value) is not int or not GENESIS_SLOT <= value < 2**63-1:
        raise ValueError('Invalid historical slot')
    return value


def initial_slot(checkpoint=None, requested=None):
    """A saved cursor always wins. Nonzero requests are explicit fresh overrides."""
    return slot(checkpoint if checkpoint is not None else
                requested if requested is not None else GENESIS_SLOT)


def history_available(start, floor):
    """Genesis is a boundary, not a promise of a retrievable slot-zero block."""
    slot(start)
    slot(floor)
    return floor <= max(start, GENESIS_SLOT+1)


def initialize_store(store, requested=None):
    """Persist a requested boundary, without representing it as committed data."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute("SELECT value FROM checkpoints WHERE name='next_slot'").fetchone()
        if saved is not None:
            validate_resume(db, saved[0])
            return None
        if any(db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone()
               for table in ('blocks', 'ranges', 'transactions', 'events')):
            raise ValueError('Historical data has no checkpoint; explicit recovery is required')
        start = initial_slot(requested=requested)
        db.execute("INSERT INTO checkpoints VALUES('next_slot',?)", (start,))
        return start


def validate_resume(db, cursor):
    """Keep saved state intact, but do not trust legacy unanchored skip tails."""
    initial_slot(cursor)
    end=db.execute('SELECT max(end) FROM ranges').fetchone()[0]
    if end is None:
        if db.execute('SELECT 1 FROM blocks LIMIT 1').fetchone():
            raise ValueError('Blocks without committed ranges require explicit recovery')
        return
    if end+1!=cursor:
        raise ValueError('Checkpoint disagrees with retained ranges; explicit recovery required')
    if not db.execute('SELECT 1 FROM blocks WHERE slot=?',(end,)).fetchone():
        raise ValueError('Legacy checkpoint has an unanchored historical tail; explicit recovery required')
    if db.execute('''SELECT 1 FROM (
        SELECT start,max(end) OVER (ORDER BY start ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior
        FROM ranges) WHERE prior IS NOT NULL AND start>prior+1 LIMIT 1''').fetchone():
        raise ValueError('Retained committed ranges contain an unresolved historical gap')
