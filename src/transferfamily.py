import os
import itertools
import re
import pdb
import json
from collections import defaultdict

import debug
import debug as Dbg
import config
import sqs

import config as Cfg
import debug as Dbg
from notify import record_call_prefix as R

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class TransferFamily:
    def __init__(self, context, o_state, o_cloudwatch):
        self.context     = context
        self.o_state     = o_state
        self.cloudwatch  = o_cloudwatch
        self.servers     = []
        Cfg.register({
                "transferfamily.enable,Stable": {
                    "DefaultValue": "0",
                    "Format": "Bool",
                    "Description": """Enable management of TransferFamily services.

Disabled by default to save Main Lambda execution time. This flag activates support of TransferFamily services in Static Subfleets.
                """
                },
                "transferfamily.state.default_ttl" : "hours=2",
                "transferfamily.metrics.time_resolution": "60",
            })


    def get_prerequisites(self):
        if not Cfg.get_int("transferfamily.enable"):
            log.log(log.NOTICE, "TransferFamily support currently disabled. Set 'transferfamily.enable' to '1' to enable.")
            return

        self.resources = self.o_state.get_resources(service="transfer")

        self.servers = []
        transfer_client = self.context["transfer.client"]
        paginator       = transfer_client.get_paginator('list_servers')
        tag_mappings    = itertools.chain.from_iterable(
            page['Servers']
                for page in paginator.paginate()
            )
        self.servers    = list(tag_mappings)

        #self.state_table = self.o_state.get_state_table()
        #self.state_table.register_aggregates([
        #    {
        #        "Prefix": "transferfamily.",
        #        "Compress": True,
        #        "DefaultTTL": Cfg.get_duration_secs("transferfamily.state.default_ttl"),
        #        "Exclude" : []
        #    }
        #    ])

        metric_time_resolution = Cfg.get_int("transferfamily.metrics.time_resolution")
        if metric_time_resolution < 60: metric_time_resolution = 1 # Switch to highest resolution
        self.cloudwatch.register_metric([
                { "MetricName": "StaticFleet.TransferFamily.Size",
                  "Unit": "Count",
                  "StorageResolution": metric_time_resolution },
                { "MetricName": "StaticFleet.TransferFamily.RunningServers",
                  "Unit": "Count",
                  "StorageResolution": metric_time_resolution },
                ])


    @xray_recorder.capture()
    def manage_subfleet(self):
        """Manage start/stop actions for static subfleet TransferFamily servers
        """
        if not Cfg.get_int("transferfamily.enable"):
            return

        states = defaultdict(int)
        for server in self.servers:
            arn = server["Arn"]
            subfleet_name  = self.get_static_subfleet_name(arn)
            if subfleet_name is None:
                log.warn("Missing tag 'clonesquad:static-subfleet-name' on resource %s!" % arn)
                continue
            forbidden_chars = "[ .]"
            if re.match(forbidden_chars, subfleet_name):
                log.warning("Subfleet name '%s' contains invalid characters (%s)!! Ignore %s..." % (subfleet_name, forbidden_chars, arn))
                continue
            expected_state = Cfg.get("staticfleet.%s.state" % subfleet_name, none_on_failure=True)
            if expected_state is None:
                log.warn("Encountered a static fleet TransferFamily server (%s) without matching state directive. Please set 'staticfleet.%s.state' configuration key..." % 
                        (arn, subfleet_name))
                continue
            if expected_state == "running":
                svc_expected_state = "ONLINE"
            elif expected_state == "stopped":
                svc_expected_state = "OFFLINE"
            elif expected_state in ["", "undefined"]:
                continue # Nothing to do
            else:
                log.warn("Can't understand 'staticfleet.%s.state' configuration key value! Valid values are [running, stopped, undefined]" % expected_state)
                continue

            current_state  = server["State"]
            if server["State"] == "ONLINE": current_state = "running"

            log.debug("Manage '%s': subfleet_name=%s, current_state=%s, expected_state=%s" % 
                    (arn, subfleet_name, current_state, expected_state))
            if expected_state != current_state:
                log.info("'%s' is transitionning from '%s' to '%s' state..." % (arn, current_state, expected_state))
            states[current_state] += 1

            pdb.set_trace()
            if expected_state == "running" and server["State"] == "OFFLINE":
                self.start_resource(arn, server["ServerId"])
            if expected_state == "stopped" and server["State"] == "ONLINE":
                self.stop_resource(arn, server["ServerId"])

        cw = self.cloudwatch
        if len(self.servers):
            cw.set_metric("StaticFleet.TransferFamily.Size", len(self.servers))
            cw.set_metric("StaticFleet.TransferFamily.RunningServers", states["running"] + states["STARTING"])
        else:
            cw.set_metric("StaticFleet.TransferFamily.Size", None)
            cw.set_metric("StaticFleet.TransferFamily.RunningServers", None)

    def stop_resource(self, arn, service_id):
        try:
            log.info("Stopping '%s'..." % arn)
            client  = self.context["transfer.client"]
            response = R("transferfamily", 
                    lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                    client.stop_server, ServerId=service_id)
            log.debug(Dbg.pprint(response))
        except Exception as e:
            log.warning("Got exception while stopping '%s'! : %s" % (arn, e))

    def start_resource(self, arn, service_id):
        try:
            log.info("Starting '%s'..." % arn)
            client  = self.context["transfer.client"]
            response = R("transferfamily", 
                    lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                    client.start_server, ServerId=service_id)
            log.debug(Dbg.pprint(response))
        except Exception as e:
            log.warning("Got exception while starting '%s'! : %s" % (arn, e))

    def get_tags(self, arn):
        tags = {}
        svc = next(filter(lambda s: s["ResourceARN"] == arn, self.resources), None)
        if svc is None:
            return {}
        for t in svc["Tags"]:
            tags[t["Key"]] = t["Value"]
        return tags

    def get_static_subfleet_name(self, svc):
        tags = self.get_tags(svc)
        if "clonesquad:static-subfleet-name" in tags:
            return tags["clonesquad:static-subfleet-name"]
        return None

