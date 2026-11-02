"""Event parity and fail-closed compact projection tests."""
import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_onchain_indexer as fixtures
from onchain_decode import Decoder, read_struct
from solana_rpc_compat import extract_event_batch
try:
    from _solana_binary import NativeBorshSchema
except ImportError:
    NativeBorshSchema=None


class CompactTests(unittest.TestCase):
    def test_projection_preserves_columns_and_never_grants_market_eligibility(self):
        tx=fixtures.transaction()
        expected,errors=Decoder().transaction(tx,1,0,1000)
        batch=extract_event_batch([tx],slot=1,block_time=1000,source_id='synthetic')
        actual=[{batch['columns'][i]:row[i] for i in present} for row,present in zip(batch['rows'],batch['presence'])]
        self.assertEqual(actual,expected if not errors else [])
        self.assertFalse(batch['market_eligible'])
        with self.assertRaises(ValueError):extract_event_batch([tx]*65,slot=1,block_time=1000,source_id='synthetic')

    def test_parity_mismatch_quarantines_entire_transaction(self):
        with patch.object(Decoder,'event_fields',side_effect=ValueError('Native event field parity mismatch')):
            batch=extract_event_batch([fixtures.transaction()],slot=1,block_time=1000,source_id='synthetic')
        self.assertEqual(batch['rows'],[])
        self.assertTrue(batch['issues'])


@unittest.skipUnless(NativeBorshSchema is not None,'Build rpc-engine first')
class NativeEventTests(unittest.TestCase):
    def test_recorded_extra_bytes_are_inside_event_not_cpi_wrapper(self):
        root=Path(__file__).parent/'fixtures/onchain'
        manifest=json.loads((root/'manifest.json').read_text())
        for fixture in manifest['fixtures']:
            packed=(root/fixture['file']).read_bytes()
            self.assertEqual(hashlib.sha256(packed).hexdigest(),fixture['sha256'])
            tx=json.loads(packed)
            logs={base64.b64decode(line[len('Program data: '):],validate=True)
                  for line in tx['meta']['logMessages'] if line.startswith('Program data: ')}
            interpretations=[]
            for backend in ('python','rust','compare'):
                decoder=Decoder(backend)
                original=decoder.event_fields
                prefixes=[]
                def observe(program,name,raw):
                    fields,end=original(program,name,raw)
                    prefixes.append((program,name,raw,fields,end))
                    return fields,end
                with patch.object(decoder,'event_fields',side_effect=observe):
                    events,issues=decoder.transaction(tx,fixture['slot'],fixture['transaction_index'],tx['blockTime'])
                self.assertFalse(events)
                self.assertIn('Unreviewed event layout/version',issues)
                self.assertTrue(prefixes)
                for program,name,raw,fields,end in prefixes:
                    self.assertIn(raw[8:],logs)
                    self.assertEqual(len(raw)-end,8)
                interpretations.append(prefixes)
            self.assertEqual(interpretations[0],interpretations[1])
            self.assertEqual(interpretations[0],interpretations[2])

    def test_synthetic_both_programs_and_recorded_layout_quarantine(self):
        inputs=[fixtures.transaction(venue=venue,buy=buy) for venue in ('pump','pump_amm') for buy in (True,False)]
        inputs += [json.loads(Path('tests/fixtures/onchain/'+name+'.json').read_text()) for name in ('curve','pumpswap')]
        for tx in inputs:
            expected=Decoder().transaction(tx,1,0,1000)
            for backend in ('rust','compare'):
                with self.subTest(backend=backend):self.assertEqual(Decoder(backend).transaction(tx,1,0,1000),expected)

    def test_bad_bounds_boolean_and_schema(self):
        schema=NativeBorshSchema('[{"name":"flag","type":"bool"}]','{}')
        for raw,offset in [(b'',0),(b'\2',0),(b'\0',2),(b'\0'*65537,0)]:
            with self.assertRaises(ValueError):schema.decode(raw,offset)
        with self.assertRaises(ValueError):NativeBorshSchema('[]','[]')

    def test_large_integer_is_lossless(self):
        schema=NativeBorshSchema('[{"name":"amount","type":"u128"}]','{}')
        text,end=schema.decode(bytes([255])*16,0)
        self.assertEqual(int(json.loads(text)['amount']['$integer']),2**128-1)
        self.assertEqual(end,16)
