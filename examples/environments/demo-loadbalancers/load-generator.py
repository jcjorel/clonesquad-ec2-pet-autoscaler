#!/usr/bin/python3

from os.path import dirname, abspath, join
import sys

# Find code directory relative to our directory
THIS_DIR = dirname(__file__)
CODE_DIR = abspath(join(THIS_DIR, '../../..', 'src'))
sys.path.append(CODE_DIR)

import sys
from jinja2 import Template
import argparse
import pdb
import json
import misc
import os
import math

parser = argparse.ArgumentParser(description="LoadBalancer load generator")
parser.add_argument('loadbalancer_url', help="LoadBalancer URL", type=str, nargs=1)
parser.add_argument('--period', help="Duration", type=str, default="hours=2")
parser.add_argument('--max-concurrency', help="Connection concurrency to load balancer", type=int, default=50)

args = parser.parse_args()
args_dict = {}
for a in args._get_kwargs():
    args_dict[a[0]] = a[1]

period          = misc.str2duration_seconds(args.period)
time_offset     = misc.seconds_from_epoch_utc()
max_concurrency = args.max_concurrency

while True:
    now         = misc.seconds_from_epoch_utc()
    seconds     = now - time_offset
    concurrency = 1 + int((max_concurrency - 1) * 
            ((1 - math.cos(2 * math.pi * (seconds % period) / period) ) / 2.0)
        )
    
    cmd = "ab -c %(concurrency)s -n %(concurrency)s %(loadbalancer_url)s" % {
        "concurrency": concurrency,
        "loadbalancer_url": args.loadbalancer_url[0]
        }
    print(cmd)
    os.system(cmd)


