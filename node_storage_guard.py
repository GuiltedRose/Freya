"""Stop the bound node before configured free-space exhaustion; never delete data."""
import argparse
import os
import time


def check(paths,minimum_bytes):
    if type(minimum_bytes) is not int or minimum_bytes<=0:raise ValueError('Positive storage reserve required')
    for path in paths:
        space=os.statvfs(path)
        if space.f_bavail*space.f_frsize<minimum_bytes:
            raise RuntimeError('Node storage reserve reached: '+str(path))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',action='append',required=True)
    parser.add_argument('--minimum-bytes',type=int,required=True)
    parser.add_argument('--interval',type=int,default=15)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if not 5<=args.interval<=60:parser.error('Interval must be 5..60 seconds')
    while True:
        check(args.path,args.minimum_bytes)
        if args.once:return
        time.sleep(args.interval)


if __name__=='__main__':main()
