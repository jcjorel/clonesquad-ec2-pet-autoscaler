""" ssm.py

License: MIT

"""
import boto3
import json
import pdb
import re
import sys
import yaml
from datetime import datetime
from datetime import timedelta
from collections import defaultdict
from botocore.exceptions import ClientError

import misc
import ec2
import config as Cfg
import debug as Dbg
from notify import record_call as R
from notify import record_call_extended as R_xt

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class SSM:
    @xray_recorder.capture(name="SSM.__init__")
    def __init__(self, context):
        self.context                 = context
        self.o_state                 = self.context["o_state"]
        self.maintenance_windows     = {}
        self.o_ec2                   = self.context["o_ec2"]
        GroupName                    = self.context["GroupName"]

        Cfg.register({
            "ssm.state.default_ttl": "hours=2",
            "ssm.maintenance_window.start_ahead": "minutes=15",
            "ssm.maintenance_window.defaults": "CS-{GroupName}",
            "ssm.maintenance_window.mainfleet.defaults": "CS-{GroupName}-__main__",
            "ssm.maintenance_window.subfleet.__all__.defaults": "CS-{GroupName}-__all__",
            "ssm.maintenance_window.subfleet.{SubfleetName}.defaults": "CS-{GroupName}-{SubfleetName}"
            })

        self.o_state.register_aggregates([
            {
                "Prefix": "ssm.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("ssm.state.default_ttl"),
                "Exclude" : [
                    ]
            }
            ])

    def get_prerequisites(self):
        """ Gather instance status by calling SSM APIs.
        """
        GroupName = self.context["GroupName"]
        for SubfleetName in self.o_ec2.get_subfleet_names():
            Cfg.register({
                f"ssm.maintenance_window.subfleet.{SubfleetName}.defaults": f"CS-{GroupName}-{SubfleetName}"
            })

        misc.initialize_clients(["ssm"], self.context)
        client = self.context["ssm.client"]

        # Retrive all SSM maintenace windows applicable to this CloneSquad deployment
        mw_names = {
            "__default__": {},
            "__main__": {},
            "__all__":  {}
        }

        fmt                              = self.context.copy()
        mw_names["__default__"]["Names"] = Cfg.get_list("ssm.maintenance_window.defaults", fmt=fmt)
        mw_names["__main__"]["Names"]    = Cfg.get_list("ssm.maintenance_window.mainfleet.defaults", fmt=fmt)
        mw_names["__all__"]["Names"]     = Cfg.get_list("ssm.maintenance_window.subfleet.__all__.defaults", fmt=fmt)

        all_mw_names = mw_names["__default__"]["Names"]
        all_mw_names.extend([ n for n in mw_names["__main__"]["Names"] if n not in all_mw_names])
        all_mw_names.extend([ n for n in mw_names["__all__"]["Names"] if n not in all_mw_names])
        for SubfleetName in self.o_ec2.get_subfleet_names():
            mw_names[f"SubfleetName.{SubfleetName}"] = {}
            mw_names[f"SubfleetName.{SubfleetName}"]["Names"] = Cfg.get_list(f"ssm.maintenance_window.subfleet.{SubfleetName}.defaults", fmt=fmt)
            all_mw_names.extend([ n for n in mw_names[f"SubfleetName.{SubfleetName}"]["Names"] if n not in all_mw_names])


        names = all_mw_names
        mws   = []
        while len(names):
            paginator = client.get_paginator('describe_maintenance_windows')
            response_iterator = paginator.paginate(
                Filters=[
                    {
                        'Key': 'Name',
                        'Values': names[:20]
                    },
                ])
            for r in response_iterator:
                mws.extend([d for d in r["WindowIdentities"] if d["Enabled"] and d not in mws])
            names = names[20:]
        # Make string dates as object dates
        for d in mws:
            d["NextExecutionTime"] = misc.str2utc(d["NextExecutionTime"])
        self.maintenance_windows = {
            "Names": mw_names,
            "Windows": mws
        }
        if len(mws):
            log.log(log.NOTICE, f"Found matching SSM maintenance windows: %s" % self.maintenance_windows)

    def _get_maintenance_window_for_fleet(self, fleet=None):
        default_names             = self.maintenance_windows["__default__"]["Names"]
        main_default_names        = self.maintenance_windows["__main__"]["Names"]
        subfleet_default_names    = self.maintenance_windows["__main__"]["Names"]
        mws                       = self.maintenance_windows["Windows"]
        names                     = []
        if not fleet:
            names = main_default_names if len(main_default_names) else default_names
        else:
            subfleet_names = self.maintenance_windows[f"SubfleetName.{SubfleetName}"]["Names"]
            names = subfleet_names if len(subfleet_names) else subfleet_default_names if len(subfleet_default_names) else default_names
        return [w for w in mws if w["Name"] in names]


    def is_maintenance_time(self, fleet=None):
        pdb.set_trace()
        now         = self.context["now"]
        start_ahead = Cfg.get_duration_secs("ssm.maintenance_window.start_ahead")
        windows     = self._get_maintenance_window_for_fleet(fleet=fleet)
        for w in windows:
            pass

