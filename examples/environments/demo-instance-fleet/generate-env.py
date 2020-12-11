#!/usr/bin/python3

from os.path import dirname, abspath, join
import sys

# Find code directory relative to our directory
THIS_DIR = dirname(__file__)
CODE_DIR = abspath(join(THIS_DIR, '../../..', 'src'))
sys.path.append(CODE_DIR)

import os
import sys
from jinja2 import Template
import argparse
import json
import hashlib
import pdb
import misc

def convert(args):
    for a in args:
        if "Spot" in a: a["Spot"] = bool(a["Spot"])
        if "Count" in a: a["Count"] = int(a["Count"])
        a["type"] = "DBCluster" if a["Engine"].startswith("aurora") else "DBInstance"
    return args


parser = argparse.ArgumentParser(description="Generate CloudFormation template for Test Environments")
parser.add_argument('--specs', help="Instances specifications", type=str, default="")
parser.add_argument('--subnet_count', help="Number of Persistent Spot instances", type=int, default=3)
parser.add_argument('--subfleet-specs', help="fleet Instances specifications", type=str, default="")
parser.add_argument('--subfleet-rds-specs', help="fleet RDS specifications", type=str, default="")

args = parser.parse_args()
args_dict = {}
for a in args._get_kwargs():
    args_dict[a[0]] = a[1]

oneliner_args           = convert(misc.parse_line_as_list_of_dict(args.specs, leading_keyname="InstanceType"))
oneliner_fleet_args     = convert(misc.parse_line_as_list_of_dict(args.subfleet_specs, leading_keyname="InstanceType"))
oneliner_fleet_rds_args = convert(misc.parse_line_as_list_of_dict(args.subfleet_rds_specs, leading_keyname="Engine"))
seed     = hashlib.md5()
seed.update(bytes(os.environ["AWS_ACCOUNT_ID"], "utf-8"))
# Look for more entropy
with open("%s/deployment-parameters.txt" % os.environ["CLONESQUAD_PARAMETER_DIR"]) as f:
    seed.update(bytes(f.read(), "utf-8"))
user     = "user%s" % seed.hexdigest()[:8] 
seed.update(bytes("password", "utf-8"))
password = "password%s" % seed.hexdigest()[:16] 
args_dict["parameters"] = {
            "nb_of_instance_specs": len(oneliner_args),
            "specs": oneliner_args,
            "nb_of_subfleets": len(oneliner_fleet_args),
            "subfleet_specs": oneliner_fleet_args,
            "nb_of_rds_fleets": len(oneliner_fleet_rds_args),
            "subfleet_rds_spec" : oneliner_fleet_rds_args,
            "user": user,
            "password": password,
        }
print(json.dumps(args_dict, indent=4, sort_keys=True, default=str), file=sys.stderr)

with open('template.yaml') as file_:
    template = Template(file_.read())
r = template.render(**args_dict)

print(r)

