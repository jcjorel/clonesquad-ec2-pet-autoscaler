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

parser = argparse.ArgumentParser(description="Generate CloudFormation template for Test Environments")
parser.add_argument('loadbalancers', help="LoadBalancer specifications", type=str, nargs=1)

args = parser.parse_args()
args_dict = {}
for a in args._get_kwargs():
    args_dict[a[0]] = a[1]

oneliner_args = misc.parse_line_as_list_of_dict(args.loadbalancers[0], leading_keyname="name")

uniq = []
[ uniq.append(spec["port"]) for spec in oneliner_args if spec["port"] not in uniq]
args_dict["ports"] = uniq

args_dict["loadbalancers"] = oneliner_args
print(json.dumps(args_dict, indent=4, sort_keys=True, default=str), file=sys.stderr)

with open('template.yaml') as file_:
    template = Template(file_.read())
r = template.render(**args_dict)

print(r)

