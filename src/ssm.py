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
            "ssm.enable,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Enable globally support for AWS System Manager by CloneSquad.

CloneSquad can leverage AWS SSM to take into account Maintenance Windows and use SSM RunCommand to execute status probe scripts located in managed instances.
            """
            },
            "ssm.feature.ec2.instance_ready_for_shutdown,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Ensure instance shutdown readiness with /etc/cs-ssm/instance-ready-for-shutdown script on SSM managed instances."

This enables support for direct sensing of instance shutdown readiness based on the return code of a script located in each EC2 instances. When set to 1, CloneSquad sends a SSM RunCommand to a managed instance candidate prior to shutdown: 
* If /etc/cs-ssm/instance-ready-for-shutdown is present, it is executed with the SSM agent daemon user rights: If the script returns a NON-zero code, Clonesquad will postpone the instance shutdown and will call this script again after 2 * [ `app.run_period`](#apprun_period) seconds...
* If /etc/cs-ssm/instance-ready-for-shutdown is NOT present, immediate shutdown readyness is assumed.

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
             "ssm.feature.ec2.instance_ready_for_shutdown.max_shutdown_delay,Stable": {
                     "DefaultValue": "hours=1",
                     "Format": "Duration",
                     "Description": """ Maximum time to spend waiting for SSM based ready-for-shutdown status.

When SSM support is enabled with [`ssm.feature.ec2.instance_ready_for_operation`](#ssmfeatureec2instance_ready_for_operation), instances may notify CloneSquad when they are ready for shutdown. This setting defines
the maximum time spent by CloneSquad to receive this signal before to forcibly shutdown the instance.
                """
             },
            "ssm.feature.ec2.instance_ready_for_operation,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Ensure an instance go out from 'initializing' state based on an instance script returns code.

This enables support for direct sensing of instance **serving** readiness based on the return code of a script located in each EC2 instances. CloneSquad never stops an instance in the 'initializing' state. This state is normally automatically left after [`ec2.schedule.start.warmup_delay`](#ec2schedulestartwarmup_delay) seconds: When this setting is set, an SSM command is sent to each instance and call a script to get a direct ack that an instance can left the 'initializing' state.

* If `/etc/cs-ssm/instance-ready-for-operation` is present, it is executed with the SSM agent daemon user rights: If the script returns a NON-zero code, Clonesquad will postpone the instance go-out from 'initializing' state and will call this script again after 2 * [ `app.run_period`](#apprun_period) seconds...
* If `/etc/cs-ssm/instance-ready-for-operation` is NOT present, the instance leaves the 'initializing' state immediatly after 'warmup delay'..

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
            "ssm.feature.ec2.instance_ready_for_operation.max_initializing_time,Stable": {
                "DefaultValue": "hours=1",
                "Format": "Duration",
                "Description": """Max time that an instance can spend in 'initializing' state.

When [`ssm.feature.ec2.instance_ready_for_operation`](#ssmfeatureec2instance_ready_for_operation) is set, this setting defines the maximum duration that CloneSquas will attempt to get a status 'ready-for-operation' for a specific instance through SSM RunCommand calls and execution of the `/etc/cs-ssm/instance-ready-for-operation` script.
            """
            },
            "ssm.feature.ec2.instance_healthcheck": "0",
            "ssm.feature.ec2.maintenance_window,Stable": {
                "DefaultValue": "1",
                "Format": "Bool",
                "Description": """Defines if SSM maintenance window support is activated.

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
            "ssm.feature.ec2.maintenance_window.subfleet.force_running,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Defines if a subfleet is forcibly set to 'running' when a maintenance window is actice.
        
            By default, the subfleet is not waken up by a maintenance window if the current subfleet state is in 'stopped' or 'undefined' state.
            """,
            },
            "ssm.state.default_ttl": "hours=1",
            "ssm.state.command.default_ttl": "minutes=10",
            "ssm.state.command.result.default_ttl": "minutes=5",
            "ssm.maintenance_window.start_ahead,Stable": {
                    "DefaultValue": "minutes=15",
                    "Format": "Duration",
                    "Description": """Start instances this specified time ahead of the next Maintenance Window.

In order to ensure that instances are up and ready when a SSM Maintenance Window starts, they are started in advance of the 'NextExecutionTime' defined in the maintenance window.
            """
            },
            "ssm.maintenance_window.global_defaults": "CS-GlobalDefaultMaintenanceWindow",
            "ssm.maintenance_window.defaults": "CS-{GroupName}",
            "ssm.maintenance_window.mainfleet.defaults": "CS-{GroupName}-__main__",
            "ssm.maintenance_window.mainfleet.ec2.schedule.min_instance_count": {
                    "DefaultValue": "100%",
                    "Format": "IntegerOrPercentage",
                    "Description": """Minimum number of instances serving in the fleet when the Maintenance Window occurs.

> Note: If this value is set to the special value '100%', the setting [`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count) is also forced to '100%'. This implies that any LightHouse instances will also be started and full fleet stability ensured during the Maintenance Window.
            """
            },
            "ssm.maintenance_window.subfleet.__all__.defaults": "CS-{GroupName}-Subfleet.__all__",
            "ssm.maintenance_window.subfleet.{SubfleetName}.defaults": "CS-{GroupName}-Subfleet.{SubfleetName}",
            "ssm.maintenance_window.subfleet.{SubfleetName}.ec2.schedule.min_instance_count": {
                    "DefaultValue": "100%",
                    "Format": "IntegerOrPercentage",
                    "Description": """Minimum number of instances serving in the fleet when the Maintenance Window occurs.

> Note: If this value is set to the special value '100%', the setting [`subfleet.{subfleet}.ec2.schedule.desired_instance_count`](#subfleetsubfleetec2scheduledesired_instance_count) is also forced to '100%' ensuring full subfleet stability.
            """
            }
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
            log.log(log.NOTICE, "SSM support is currently disabled. Set ssm.enable to 1 to enabled it.")
            return
        now       = self.context["now"]
        ttl       = Cfg.get_duration_secs("ssm.state.default_ttl")
        GroupName = self.context["GroupName"]

        misc.initialize_clients(["ssm"], self.context)
        client = self.context["ssm.client"]

        # Retrive all SSM maintenace windows applicable to this CloneSquad deployment
        mw_names = {
            "__globaldefault__": {},
            "__default__": {},
            "__main__": {},
            "__all__":  {}
        }

        fmt                              = self.context.copy()
        mw_names["__globaldefault__"]["Names"] = Cfg.get_list("ssm.maintenance_window.global_defaults", fmt=fmt)
        mw_names["__default__"]["Names"] = Cfg.get_list("ssm.maintenance_window.defaults", fmt=fmt)
        mw_names["__main__"]["Names"]    = Cfg.get_list("ssm.maintenance_window.mainfleet.defaults", fmt=fmt)
        mw_names["__all__"]["Names"]     = Cfg.get_list("ssm.maintenance_window.subfleet.__all__.defaults", fmt=fmt)

        all_mw_names = mw_names["__globaldefault__"]["Names"]
        all_mw_names.extend([ n for n in mw_names["__default__"]["Names"] if n not in all_mw_names])
        all_mw_names.extend([ n for n in mw_names["__main__"]["Names"] if n not in all_mw_names])
        all_mw_names.extend([ n for n in mw_names["__all__"]["Names"] if n not in all_mw_names])
        for SubfleetName in self.o_ec2.get_subfleet_names():
            fmt["SubfleetName"] = SubfleetName
            mw_names[f"Subfleet.{SubfleetName}"] = {}
            Cfg.register({
                f"ssm.maintenance_window.subfleet.{SubfleetName}.defaults": Cfg.get("ssm.maintenance_window.subfleet.{SubfleetName}.defaults"),
                f"ssm.maintenance_window.subfleet.{SubfleetName}.ec2.schedule.min_instance_count": 
                    Cfg.get("ssm.maintenance_window.subfleet.{SubfleetName}.ec2.schedule.min_instance_count")
            })
            mw_names[f"Subfleet.{SubfleetName}"]["Names"] = Cfg.get_list(f"ssm.maintenance_window.subfleet.{SubfleetName}.defaults", fmt=fmt)
            all_mw_names.extend([ n for n in mw_names[f"Subfleet.{SubfleetName}"]["Names"] if n not in all_mw_names])


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
                for wi in r["WindowIdentities"]:
                    if not wi["Enabled"]:
                        log.log(log.NOTICE, f"SSM Maintenance Window '%s' not enabled. Ignored..." % wi["Name"])
                        continue
                    if "NextExecutionTime" not in wi:
                        log.log(log.NOTICE, f"SSM Maintenance Window '%s' without 'NextExecutionTime'. Ignored..." % wi["Name"])
                        continue
                    if wi not in mws:
                        mws.append(wi)
            names = names[20:]
        # Make string dates as object dates
        for d in mws:
            d["NextExecutionTime"] = misc.str2utc(d["NextExecutionTime"])
        self.maintenance_windows = {
            "Names": mw_names,
            "Windows": mws
        }

        # Retrieve Maintenace Window tags with the resourcegroup API
        tagged_mws = self.context["o_state"].get_resources(service="ssm", resource_name="maintenancewindow")
        for tmw in tagged_mws:
            mw_id = tmw["ResourceARN"].split("/")[1]
            mw = next(filter(lambda w: w["WindowId"] == mw_id, mws), None)
            if mw:
                mw["Tags"] = tmw["Tags"]
        for mw in mws:
            if "Tags" not in mw:
                log.warning(f"Please tag SSM Maintenance Window '%s/%s' with 'clonesquad:group-name': '%s'!" %
                        (mw["Name"], mw["WindowId"], self.context["GroupName"]))

        self.manage_maintenance_windows()
        if len(mws):
            log.log(log.NOTICE, f"Found matching SSM maintenance windows: %s" % self.maintenance_windows["Windows"])


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

    #####################################################################
    ## SSM RunCommand support
    #####################################################################

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
                                "*Cause: SSM exec error? started shell command too verbose? (please limit to 24kBytes max!)")
                        agent_status = "CLONESQUAD-SSM-AGENT-STATUS:"
                        status_msg  = next(filter(lambda s: s.startswith(agent_status), stdout), None)
                        if status_msg is None:
                            status_msg = "ERROR"
                        else:
                            status_msg = status_msg[len(agent_status):]
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
            if pending_cmd is None and i not in former_results:
                non_pending_ids.append(i)
                if not return_former_results or not isinstance(former_result_status, str):
                    continue
            if former_results.get(i,{}).get(f"{command};{args}"):
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



    #####################################################################
    ## SSM Maintenance Window support
    #####################################################################

    def _get_maintenance_window_for_fleet(self, fleet=None):
        default_names             = self.maintenance_windows["Names"]["__default__"]["Names"]
        main_default_names        = self.maintenance_windows["Names"]["__main__"]["Names"]
        subfleet_default_names    = self.maintenance_windows["Names"]["__all__"]["Names"]
        mws                       = self.maintenance_windows["Windows"]
        names                     = default_names
        if not fleet:
            if len([w for w in mws if w["Name"] in main_default_names]):
                names = main_default_names
        else:
            if len([w for w in mws if w["Name"] in subfleet_default_names]):
                names = subfleet_default_names
            subfleet_names = self.maintenance_windows["Names"][f"Subfleet.{fleet}"]["Names"]
            if len([w for w in mws if w["Name"] in subfleet_names]):
                names = subfleet_names
        return [w for w in mws if w["Name"] in names]


    def is_maintenance_time(self, fleet=None, matching_window=None):
        if not self.is_feature_enabled("ec2.maintenance_window"):
            return False
        now         = self.context["now"]
        start_ahead = timedelta(seconds=Cfg.get_duration_secs("ssm.maintenance_window.start_ahead"))
        windows     = self._get_maintenance_window_for_fleet(fleet=fleet)
        for w in windows:
            end_time = w["NextExecutionTime"] + timedelta(hours=int(w["Duration"]))
            if now >= (w["NextExecutionTime"] - start_ahead) and now < end_time:
                if matching_window: matching_window.append(w)
                return True
        return False

    def manage_maintenance_windows(self):
        config_tag = "clonesquad:config:"
        def _set_tag(fleet, config, mw):
            pdb.set_trace()
            min_instance_count = None
            if "Tags" in mw:
                tags = {}
                for t in tags:
                    if t.startswith(config_tag):
                        tags[t["Key"][len(config_tag):]] = t["Value"]
                if fleet:
                    if "ec2.schedule.min_instance_count" in tags: 
                        min_instance_count = tags["ec2.schedule.min_instance_count"]
                else:
                    tag = f"subfleet.{fleet}.ec2.schedule.min_instance_count"
                    if tag in tags:
                        min_instance_count = tags[tag]
                        del tags[tag]
                    tag = f"subfleet.__all__.ec2.schedule.min_instance_count"
                    if tag in tags:
                        min_instance_count = tags[tag]
                        del tags[tag]
                for t in tags:    
                    config.set(t, tags[t])
            return min_instance_count
        config          = {}
        matching_window = []
        if self.is_maintenance_time(fleet=None, matching_window=matching_window):
            min_instance_count = _set_tag(None, config, matching_window[0])
            if min_instance_count is None:
                min_instance_count = Cfg.get("ssm.maintenance_window.mainfleet.ec2.schedule.min_instance_count")
            config["ec2.schedule.min_instance_count"] = min_instance_count
            if min_instance_count == "100%":
                config["ec2.schedule.desired_instance_count"] = "100%"

        for subfleet in self.o_ec2.get_subfleet_names():
            if self.is_maintenance_time(fleet=subfleet, matching_window=matching_window):
                min_instance_count = _set_tag(subfleet, config, matching_window[0])
                if min_instance_count:
                    min_instance_count = Cfg.get(f"ssm.maintenance_window.subfleet.{subfleet}.ec2.schedule.min_instance_count")
                config[f"subfleet.{subfleet}.ec2.schedule.min_instance_count"] = min_instance_count
                if min_instance_count == "100%":
                    config[f"subfleet.{subfleet}.ec2.schedule.desired_instance_count"] = "100%"
        Cfg.register(config, layer="SSM Maintenance window override", create_layer_when_needed=True)


