#!/usr/bin/python3

import os
from os.path import dirname, abspath, join
import sys

# Find code directory relative to our directory
THIS_DIR = dirname(__file__)
CODE_DIR = abspath(join(THIS_DIR, '../../..', 'src'))
sys.path.insert(0, CODE_DIR)
CLONESQUAD_DEPENDENCY_DIR = abspath(os.getenv("CLONESQUAD_DEPENDENCY_DIR"))
sys.path.append(CLONESQUAD_DEPENDENCY_DIR)

import sys
from jinja2 import Template
import argparse
import json
import math
import pdb
from collections import defaultdict
import debug as Dbg

parser = argparse.ArgumentParser(description="Generate CloudFormation template for Test Environments")
parser.add_argument('--period', help="Wave period in hours", type=int, default=8)
parser.add_argument('--increment', help="Increment in percent", type=int, default=5)
parser.add_argument('--with-parameterset', help="Use parameter sets instead of direct config overwrite", type=bool, default=False)
parser.add_argument('--demoname', help="Demo name (used for file output prefix)", type=str, default="demo")

args = parser.parse_args()
args_dict = {}
for a in args._get_kwargs():
    args_dict[a[0]] = a[1]

c = defaultdict(dict)
for test_case in ["min_instance_count", "desired_instance_count"]:
    c[test_case]["cron"]  = open('%s-%s-cronfile.yaml' % (args.demoname, test_case), 'w')
    if args.with_parameterset:
        c[test_case]["config"] = open('%s-%s-configfile.yaml' % (args.demoname, test_case), 'w')

period     = 3600 * args.period
steps      = 10000
planning   = {} 
last_value = -1
# Generate Sin wave
for inc in range(0,steps):
    increments = float(inc) / steps
    angle      = 2 * math.pi * increments
    value      = 100 * ((math.sin(angle) + 1.0) / 2)
    v          = int(value)
    sec_inc    = int(period * increments)
    if v != last_value and v % 5 == 0:
        last_value = v
        hours   = int(sec_inc / 3600)
        minutes = int((sec_inc - (hours * 3600)) / 60)
        h_l     = []
        h       = hours
        while h < 24: 
            h_l.append(str(h))
            h += args.period

        cron_spec  = "cron(%s %s * * ? *)" % (minutes, "\\\\,".join(h_l))
        alarm_name = "%s-%s-%s-%d%%" % (args.demoname, hours, minutes, v)

        for test_case in c.keys():
            t_c = c[test_case]
            cron_file, config_file = (t_c["cron"], t_c["config"] if "config" in t_c else None)

            parameter_set_name="pset-%d" % (v)

            if test_case == "min_instance_count":
                if args.with_parameterset:
                    cron_file.write('%s: "%s,config.active_parameter_set=%s"\n' % (alarm_name, cron_spec, parameter_set_name))
                    config_file.write("\n".join([
                        "%s:" % parameter_set_name,
                        "  ec2.schedule.scalein.rate: 10",
                        "  ec2.schedule.desired_instance_count: -1",
                        "  ec2.schedule.min_instance_count: %s%%\n" % (v)
                    ]))
                else:
                    cron_file.write('%s: "%s,ec2.schedule.min_instance_count=%s%%"\n' % (alarm_name, cron_spec, v))

            if test_case == "desired_instance_count":
                if args.with_parameterset:
                    cron_file.write('%s: "%s,config.active_parameter_set=%s"\n' % (alarm_name, cron_spec, parameter_set_name))
                    config_file.write("\n".join([
                        "%s:" % parameter_set_name,
                        "  ec2.schedule.desired_instance_count: %s%%" % v,
                        "  ec2.schedule.min_instance_count: 2\n"
                    ]))
                else:
                    cron_file.write('%s: "%s,ec2.schedule.desired_instance_count=%s%%"\n' % (alarm_name, cron_spec, v))


cron_file.close()
if args.with_parameterset:
    config_file.close()

