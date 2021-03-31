""" ssm.py

License: MIT

"""
import boto3
import json
import pdb
import re
import io
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
            "ssm.enable": "0",
            "ssm.feature.ec2.instance_ready_for_shutdown": "0",
            "ssm.feature.ec2.instance_ready_for_operation": "0",
            "ssm.feature.ec2.instance_healthcheck": "0",
            "ssm.state.default_ttl": "hours=1",
            "ssm.state.command.default_ttl": "minutes=10",
            "ssm.state.command.result.default_ttl": "minutes=5",
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

    @xray_recorder.capture()
    def get_prerequisites(self):
        """ Gather instance status by calling SSM APIs.
        """
        if not Cfg.get_int("ssm.enable"):
            log.log(log.NOTICE, "SSM is currently disabled. Set ssm.enable to 1 to enabled it.")
            return
        now       = self.context["now"]
        ttl       = Cfg.get_duration_secs("ssm.state.default_ttl")
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

        # Update instance inventory
        paginator = client.get_paginator('describe_instance_information')
        response_iterator = paginator.paginate(
            Filters=[
                {
                    'Key': 'tag:clonesquad:group-name',
                    'Values': [GroupName]
                },
            ])

        self.instance_infos = []
        for r in response_iterator:
            self.instance_infos.extend([d for d in r["InstanceInformationList"]])
        instance_info_ids = [i["InstanceId"] for i in self.instance_infos]

        instances           = self.o_ec2.get_instances(State="pending,running")
        instance_ids        = [i["InstanceId"] for i in instances]
        for info in self.o_state.get_state_json("ssm.instance_infos", default=[]):
            instance_id    = info["InstanceId"]
            if instance_id in instance_info_ids:
                # We just recived an update...
                continue
            instance       = next(filter(lambda i: i["InstanceId"] == instance_id, instances), None)
            if instance is None:
                # This instance is not more pending or running...
                continue
            last_ping_time = misc.str2utc(info["LastPingDateTime"])
            if instance["LaunchTime"] < last_ping_time and (last_ping_time - now) < timedelta(seconds=ttl):
                self.instance_infos.append(info)
        # Remove useless fields
        for info in self.instance_infos:
            if "AssociationStatus" in info:   del info["AssociationStatus"]
            if "AssociationOverview" in info: del info["AssociationOverview"]
            if "IPAddress" in info:           del info["IPAddress"]
            if "ComputerName" in info:        del info["ComputerName"]
        self.o_state.set_state_json("ssm.instance_infos", self.instance_infos, compress=True, TTL=ttl)
        
        # Update asynchronous results from previously launched commands
        self.update_pending_command_statuses()


    def is_feature_enabled(self, feature):
        if not Cfg.get_int("ssm.enable"):
            return False
        return Cfg.get_int(f"ssm.feature.{feature}")

    def is_instance_online(self, i):
        if isinstance(i, str):
            i = self.o_ec2.get_instance_by_id(i)
        instance_id = i["InstanceId"]
        launch_time = i["LaunchTime"]
        return next(filter(lambda i: i["InstanceId"] == instance_id and i["LastPingDateTime"] > launch_time and i["PingStatus"] == "Online", self.instance_infos), None) 

    @xray_recorder.capture()
    def update_pending_command_statuses(self):
        client = self.context["ssm.client"]
        self.run_cmd_states = self.o_state.get_state_json("ssm.run_commands", default={
            "Commands": [],
            "FormerResults": {}
            })

        former_results = self.run_cmd_states["FormerResults"]
        cmds           = self.run_cmd_states["Commands"]
        for cmd in cmds:
            command = cmd["Command"]
            args    = cmd["CommandArgs"]
            if "Complete" not in cmd:
                cmd_id            = cmd["Id"]
                paginator         = client.get_paginator('list_command_invocations')
                response_iterator = paginator.paginate(CommandId=cmd_id, Details=True, MaxResults=50)
                for response in response_iterator:
                    for invoc in response["CommandInvocations"]:
                        instance_id = invoc["InstanceId"]
                        status      = invoc["Status"]
                        if (status not in ["Success", "Cancelled", "Failed", "TimedOut", "Undeliverable", 
                                "Terminated", "Delivery Timed Out", "Execution Timed Out"]):
                            continue
                        stdout      = [s.rstrip() for s in io.StringIO(invoc["CommandPlugins"][0]["Output"]).readlines() 
                                if s.startswith("CLONESQUAD-SSM-AGENT-")]
                        bie_msg     = next(filter(lambda s: s.startswith("CLONESQUAD-SSM-AGENT-BIE:"), stdout), None)
                        if not bie_msg:
                            log.warning(f"Truncated reply from SSM Command Invocation ({cmd_id}/{instance_id}). "
                                "Started shell command too verbose? (please limit to 24kBytes max!)")
                        status_msg  = next(filter(lambda s: s.startswith("CLONESQUAD-SSM-AGENT-STATUS:"), stdout), None)
                        if status_msg is None:
                            status_msg = "ERROR"
                        else:
                            status_msg = status_msg[len("CLONESQUAD-SSM-AGENT-STATUS:"):]
                        details_msg = list(filter(lambda s: s.startswith("CLONESQUAD-SSM-AGENT-DETAILS:"), stdout))
                        warning_msg = list(filter(lambda s: ":WARNING:" in s, stdout))

                        result = {
                            "SSMInvocationStatus": status,
                            "Output": stdout,
                            "Status": status_msg,
                            "Details": details_msg,
                            "Warning": warning_msg,
                            "Truncated": bie_msg is None,
                            "Expiration": misc.seconds_from_epoch_utc() + Cfg.get_duration_secs("ssm.state.command.result.default_ttl")
                        }
                        # Keep track if the former result list
                        if instance_id not in former_results: former_results[instance_id] = {}
                        former_results[instance_id][f"{command};{args}"] = result
                        if instance_id not in cmd["ReceivedInstanceIds"]:
                            cmd["ReceivedInstanceIds"].append(instance_id)

                    if set(cmd["ReceivedInstanceIds"]) & set(cmd["InstanceIds"]) == set(cmd["InstanceIds"]):
                        # All invocation results received
                        cmd["Complete"] = True
        self.o_state.set_state_json("ssm.run_commands", self.run_cmd_states, compress=True, TTL=Cfg.get_duration_secs("ssm.state.default_ttl"))
        self.commands_to_send = []

    def run_command(self, instance_ids, command, args="", comment="", timeout=30, return_former_results=False):
        r = {}
        # Step 1) Check if a command is already pending for this combination of instance ids and command
        former_results  = self.run_cmd_states["FormerResults"]
        non_pending_ids = []
        for i in instance_ids:
            pending_cmd = next(filter(lambda c: c["Command"] == command and c["CommandArgs"] == args 
                and i in c["InstanceIds"], self.run_cmd_states["Commands"]), None) 
            former_result_status = former_results.get(i,{}).get(command,{}).get("Status")
            if pending_cmd is None:
                non_pending_ids.append(i)
                if not return_former_results or not isinstance(former_result_status, str):
                    continue
            if f"{command};{args}" in former_results[i]:
                r[i] = former_results[i][f"{command};{args}"]
        # Coalesce run reqs with similar command
        for i in non_pending_ids:
            cmd_to_send = next(filter(lambda c: c["Command"] == command and c["CommandArgs"] == args, self.commands_to_send), None)
            if cmd_to_send:
                if i in cmd_to_send["InstanceIds"]:
                    continue
                cmd_to_send["InstanceIds"].append(i)
            else:
                self.commands_to_send.append({
                    "InstanceIds": non_pending_ids,
                    "Command": command,
                    "CommandArgs": args,
                    "Comment": comment,
                    "Timeout": timeout,
                    })
        return r

    @xray_recorder.capture()
    def send_commands(self):        
        if not Cfg.get_int("ssm.enable"):
            return

        client = self.context["ssm.client"]
        refs   = {
            "Linux": {
                "document": "AWS-RunShellScript",
                "shell": [s.rstrip() for s in io.StringIO(str(misc.get_url("internal:cs-ssm-agent.sh"), "utf-8")).readlines()],
                "ids": [],
                "responses": []
            }
        }
        # Purge already replied results
        valid_cmds = []
        for cmd in self.run_cmd_states["Commands"]:
            if cmd.get("Complete") or cmd["Expiration"] < misc.seconds_from_epoch_utc():
                continue
            valid_cmds.append(cmd)
        self.run_cmd_states["Commands"] = valid_cmds
        # Purge outdated former results
        former_results  = self.run_cmd_states["FormerResults"]
        for i in list(former_results.keys()):
            for cmd in list(former_results[i].keys()):
                if former_results[i][cmd]["Expiration"] < misc.seconds_from_epoch_utc():
                    del former_results[i][cmd]
            if len(former_results[i].keys()) == 0:
                del former_results[i]

        # Send commands
        for cmd in self.commands_to_send:
            for i in cmd["InstanceIds"]:
                info = self.is_instance_online(i)
                if info is None:
                    continue
                pltf = refs.get(info["PlatformType"])
                if pltf is None:
                    log.warning("Can't run a command on an unsupported platform : %s" % info["PlatformType"])
                    continue # Unsupported platform
                if i not in pltf["ids"]:
                    pltf["ids"].append(i)

            for pltf_name in refs:
                pltf     = refs[pltf_name]
                if len(pltf["ids"]) == 0:
                    continue
                command  = cmd["Command"]
                args     = cmd["CommandArgs"]
                document = pltf["document"]
                shell    = pltf["shell"]
                i_ids    = pltf["ids"]
                while len(i_ids):
                    log.log(log.NOTICE, f"SSM SendCommand: {command}({args}) to %s." % i_ids[:50])
                    try:
                        response = client.send_command(
                            InstanceIds=i_ids[:50],
                            DocumentName=document,
                            TimeoutSeconds=cmd["Timeout"],
                            Comment=cmd["Comment"],
                            Parameters={
                                'commands': [l.replace("##CMD##", command).replace("##ARGS##", args) for l in shell],
                                'executionTimeout': [str(cmd["Timeout"])]
                            },
                            MaxConcurrency='100%',
                            MaxErrors='100%',
                            CloudWatchOutputConfig={
                                'CloudWatchLogGroupName': self.context["SSMLogGroup"],
                                'CloudWatchOutputEnabled': True
                            }
                        )
                        self.run_cmd_states["Commands"].append({
                            "Id": response["Command"]["CommandId"],
                            "InstanceIds": i_ids[:50],
                            "ReceivedInstanceIds": [],
                            "Command": command,
                            "CommandArgs": args,
                            "Results": {},
                            "Expiration": misc.seconds_from_epoch_utc() + Cfg.get_duration_secs("ssm.state.command.default_ttl")
                        })
                    except Exception as e:
                        # Under rare circumstance, we can receive an Exception while trying to send
                        log.log(log.NOTICE, f"Failed to do SSM SendCommand : {e}, %s" % i_ids[:50])
                    i_ids = i_ids[50:]
        self.o_state.set_state_json("ssm.run_commands", self.run_cmd_states, compress=True, TTL=Cfg.get_duration_secs("ssm.state.default_ttl"))



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
        now         = self.context["now"]
        start_ahead = Cfg.get_duration_secs("ssm.maintenance_window.start_ahead")
        windows     = self._get_maintenance_window_for_fleet(fleet=fleet)
        for w in windows:
            pass

