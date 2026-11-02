"""Read-only receipt replay. Reports decoding; never promotes history or labels."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib
from onchain_decode import Decoder


def audit(path,limit=1000,after_slot=-1,after_index=-1):
    if type(limit) is not int or not 1<=limit<=10000:
        raise ValueError('Audit limit must be 1..10000')
    path=Path(path).resolve()
    if not path.is_file():raise ValueError('Existing finalized index required')
    decoders=[Decoder(backend) for backend in ('python','rust','compare')]
    report={'purpose':'read_only_contract_replay_not_training_or_promotion',
            'source_db':str(path),'receipts':0,'clean':0,'decoded_events':0,
            'parity':True,'errors':{},'after':{'slot':after_slot,'tx_index':after_index},
            'source_admission_checked':False,'promotion_authorized':False,
            'source_db_modified':False,'paper_ready':False,
            'contracts':{program:spec['hash'] for program,spec in decoders[0].specs.items()}}
    errors=Counter()
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows=db.execute('SELECT t.raw,t.raw_hash,t.signature,t.slot,t.tx_index,b.time '
                        'FROM transactions t JOIN blocks b ON b.slot=t.slot '
                        'WHERE t.slot>? OR (t.slot=? AND t.tx_index>?) '
                        'ORDER BY t.slot,t.tx_index LIMIT ?',
                        (after_slot,after_slot,after_index,limit))
        for packed,digest,signature,slot,index,stamp in rows:
            report['receipts']+=1
            report['after']={'slot':slot,'tx_index':index}
            try:
                inflater=zlib.decompressobj()
                raw=inflater.decompress(packed,64_000_001)
                if len(raw)>64_000_000 or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                    raise ValueError('Invalid bounded saved receipt')
                if hashlib.sha256(raw).hexdigest()!=digest:
                    raise ValueError('Saved receipt hash mismatch')
                tx=json.loads(raw)
                if tx['transaction']['signatures'][0]!=signature:
                    raise ValueError('Saved receipt signature mismatch')
                results=[decoder.transaction(tx,slot,index,stamp) for decoder in decoders]
                if any(result!=results[0] for result in results[1:]):
                    report['parity']=False
                    raise ValueError('Python/Rust/compare receipt mismatch')
                events,issues=results[0]
                report['clean']+=not bool(issues)
                report['decoded_events']+=len(events)
                errors.update(issues)
            except (ValueError,KeyError,TypeError,IndexError,zlib.error) as error:
                # Unexpected values are not echoed; receipts can contain arbitrary logs.
                errors[type(error).__name__+': invalid saved receipt or decoder evidence']+=1
        report['errors']=dict(errors)
        report['page_full']=report['receipts']==limit
        return report
    finally:db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--limit',type=int,default=1000)
    parser.add_argument('--after-slot',type=int,default=-1)
    parser.add_argument('--after-index',type=int,default=-1)
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new report path; previous reports are preserved')
    result=audit(args.db,args.limit,args.after_slot,args.after_index)
    with args.output.open('x') as stream:json.dump(result,stream,indent=2,allow_nan=False);stream.write('\n')
    print(json.dumps({'report':str(args.output),'receipts':result['receipts'],
                      'clean':result['clean'],'parity':result['parity'],'history_promoted':False}))
    if result['errors'] or not result['parity']:raise SystemExit(1)


if __name__=='__main__':main()
