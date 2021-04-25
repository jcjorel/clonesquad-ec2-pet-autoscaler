""" ssm.py

License: MIT

"""
import copy
import base64
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
            "ssm.feature.events.ec2.maintenance_window_period,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Enable/Disable sending Enter/Exit Maintenance Window period events to instances.

This enables event notification support of instances when they enter or exit a SSM Maintenance Window. When set to 1, CloneSquad sends a SSM RunCommand to run the script `/etc/cs-ssm/(enter|exit)-maintenance-window-period` script located in each instances. The event is repeasted until the script returns a zero-code. If the script doesn't exist on an instance, the event is sent only once.

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
            "ssm.feature.events.ec2.instance_ready_for_shutdown,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Ensure instance shutdown readiness with /etc/cs-ssm/instance-ready-for-shutdown script on SSM managed instances."

This enables support for direct sensing of instance shutdown readiness based on the return code of a script located in each EC2 instances. When set to 1, CloneSquad sends a SSM RunCommand to a managed instance candidate prior to shutdown: 
* If `/etc/cs-ssm/instance-ready-for-shutdown` is present, it is executed with the SSM agent daemon user rights: If the script returns a NON-zero code, Clonesquad will postpone the instance shutdown and will call this script again after 2 * [ `app.run_period`](#apprun_period) seconds...
* If `/etc/cs-ssm/instance-ready-for-shutdown` is NOT present, immediate shutdown readyness is assumed.

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
             "ssm.feature.events.ec2.instance_ready_for_shutdown.max_shutdown_delay,Stable": {
                     "DefaultValue": "hours=1",
                     "Format": "Duration",
                     "Description": """ Maximum time to spend waiting for SSM based ready-for-shutdown status.

When SSM support is enabled with [`ssm.feature.events.ec2.instance_ready_for_operation`](#ssmfeatureec2instance_ready_for_operation), instances may notify CloneSquad when they are ready for shutdown. This setting defines
the maximum time spent by CloneSquad to receive this signal before to forcibly shutdown the instance.
                """
             },
            "ssm.feature.events.ec2.instance_ready_for_operation,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Ensure an instance go out from 'initializing' state based on an instance script returns code.

This enables support for direct sensing of instance **serving** readiness based on the return code of a script located in each EC2 instances. CloneSquad never stops an instance in the 'initializing' state. This state is normally automatically left after [`ec2.schedule.start.warmup_delay`](#ec2schedulestartwarmup_delay) seconds: When this setting is set, an SSM command is sent to each instance and call a script to get a direct ack that an instance can left the 'initializing' state.

* If `/etc/cs-ssm/instance-ready-for-operation` is present, it is executed with the SSM agent daemon user rights: If the script returns a NON-zero code, Clonesquad will postpone the instance go-out from 'initializing' state and will call this script again after 2 * [ `app.run_period`](#apprun_period) seconds...
* If `/etc/cs-ssm/instance-ready-for-operation` is NOT present, the instance leaves the 'initializing' state immediatly after 'warmup delay'..

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
            "ssm.feature.events.ec2.instance_ready_for_operation.max_initializing_time,Stable": {
                "DefaultValue": "hours=1",
                "Format": "Duration",
                "Description": """Max time that an instance can spend in 'initializing' state.

When [`ssm.feature.events.ec2.instance_ready_for_operation`](#ssmfeatureec2instance_ready_for_operation) is set, this setting defines the maximum duration that CloneSquas will attempt to get a status 'ready-for-operation' for a specific instance through SSM RunCommand calls and execution of the `/etc/cs-ssm/instance-ready-for-operation` script.
            """
            },
            "ssm.feature.events.ec2.scaling_state_changes,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Call a script in instance when the instance scaling state changes.

When this toggle set, the script `/etc/cs-ssm/instance-scaling-state-change` located into managed instances, is called to notify about a scaling status change. 
Currently, only `draining` and `bounced` events are sent (`bounced`is sent only if the instance bouncing feature is activated). For example, if an instance enters the `draining` state because CloneSquad wants to shutdown it, this event is called.

* If the script doesn't exists, the event is sent only once,
* If the script returns a non-zero code, the event will be repeated.

> Note: This event differs from [`ssm.feature.events.ec2.instance_ready_for_shutdown`](#ssmfeatureeventsec2instance_ready_for_shutdown) one as it is only meant to inform the instance about a status change. The [`ssm.feature.events.ec2.instance_ready_for_shutdown`](#ssmfeatureeventsec2instance_ready_for_shutdown) event is a request toward the instance asking for an approval to shutdown.

            """
            },
            "ssm.feature.events.ec2.scaling_state_changes.draining.connection_refused_tcp_ports,Stable": {
                "DefaultValue": "",
                "Format": "StringList",
                "Description": """On `draining` state, specified ports are blocked and so forbid new TCP connections (i.e. *Connection refused* message).

This features installs, **on `draining` time**, temporary iptables chain and rules denying new TCP connections to the specified port list.
This is useful, for example, to break a healthcheck life line as soon as an instance enters the `draining` state: It is especially useful when non-ELB LoadBalancers are used and CloneSquad does not know how to tell these loadbalancers that no more traffic needs to be sent to a drained instance. As it blocks only new TCP connections, currently active connections can terminate gracefully during the draining period.

> When instances are served only by CloneSquad managed ELB(s), there is no need to use this feature as CloneSquad will unregister the targets as soon as placed in `draining`state.

By default, no blocked port list is specified, so no iptables call is performed on the instance.
            """
            },
            "ssm.feature.events.ec2.scaling_state_changes.draining.{SubfleeName}.connection_refused_tcp_ports,Stable": {
                "DefaultValue": "",
                "Format": "StringList",
                "Description": """Defines the blocked TCP port list for the specified fleet.

This setting overrides the value defined in [`ssm.feature.events.ec2.scaling_state_changes.draining.connection_refused_tcp_ports`](#ssmfeatureeventsec2scaling_state_changesdrainingconnection_refused_tcp_ports) for the specified fleet.

> Use `__main__` to designate the main fleet."""
            },
            "ssm.feature.events.ec2.instance_healthcheck": "0",
            "ssm.feature.maintenance_window,Stable": {
                "DefaultValue": "0",
                "Format": "Bool",
                "Description": """Defines if SSM maintenance window support is activated.

> This setting is taken into account only if [`ssm.enable`](#ssmenable) is set to 1.
            """
            },
            "ssm.feature.maintenance_window.subfleet.{SubfleetName}.force_running,Stable": {
                "DefaultValue": "1",
                "Format": "Bool",
                "Description": """Defines if a subfleet is forcibly set to 'running' when a maintenance window is actice.
        
By default, all the subfleets is woken up by a maintenance window ([`subfleet.{SubfleetName}.state`](#subfleetsubfleetnamestate) is temprarily forced to `running`).
            """,
            },
            "ssm.state.default_ttl": "hours=24",
            "ssm.state.command.default_ttl": "minutes=10",
            "ssm.state.command.result.default_ttl": "minutes=5",
            "ssm.feature.maintenance_window.start_ahead,Stable": {
                    "DefaultValue": "minutes=15",
                    "Format": "Duration",
                    "Description": """Start instances this specified time ahead of the next Maintenance Window.

In order to ensure that instances are up and ready when a SSM Maintenance Window starts, they are started in advance of the 'NextExecutionTime' defined in the SSM maintenance window object.
            """
            },
            "ssm.feature.maintenance_window.start_ahead.max_jitter": "66%",
            "ssm.feature.maintenance_window.global_defaults": "CS-GlobalDefaultMaintenanceWindow",
            "ssm.feature.maintenance_window.defaults": "CS-{GroupName}",
            "ssm.feature.maintenance_window.mainfleet.defaults": "CS-{GroupName}-Mainfleet",
            "ssm.feature.maintenance_window.mainfleet.ec2.schedule.min_instance_count": {
                    "DefaultValue": "100%",
                    "Format": "IntegerOrPercentage",
                    "Description": """Minimum number of instances serving in the fleet when the Maintenance Window occurs.

> Note: If this value is set to the special value '100%', the setting [`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count) is also forced to '100%'. This implies that any LightHouse instances will also be started and full fleet stability ensured during the Maintenance Window.
            """
            },
            "ssm.feature.maintenance_window.subfleet.__all__.defaults": "CS-{GroupName}-Subfleet.__all__",
            "ssm.feature.maintenance_window.subfleet.{SubfleetName}.defaults": "CS-{GroupName}-Subfleet.{SubfleetName}",
            "ssm.feature.maintenance_window.subfleet.{SubfleetName}.ec2.schedule.min_instance_count": {
                    "DefaultValue": "100%",
                    "Format": "IntegerOrPercentage",
                    "Description": """Minimum number of instances serving in the fleet when the Maintenance Window occurs.

> Note: If this value is set to the special value '100%', the setting [`subfleet.{subfleet}.ec2.schedule.desired_instance_count`](#subfleetsubfleetec2scheduledesired_instance_count) is also forced to '100%' ensuring full subfleet stability.
            """
            },
            })

        self.o_state.register_aggregates([
            {
                "Prefix": "ssm.events",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("ssm.state.default_ttl"),
                "Exclude" : []
            },
            ])

    @xray_recorder.capture()
    def get_prerequisites(self):
        """ Gather instance status by calling SSM APIs.
        """
        if not Cfg.get_int("ssm.enable"):
            log.log(log.NOTICE, "SSM support is currently disabled. Set ssm.enable to 1 to enabled it.")
            return
        now       = self.context["now"]
        self.ttl  = Cfg.get_duration_secs("ssm.state.default_ttl")
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
        mw_names["__globaldefault__"]["Names"] = Cfg.get_list("ssm.feature.maintenance_window.global_defaults", fmt=fmt)
        mw_names["__default__"]["Names"] = Cfg.get_list("ssm.feature.maintenance_window.defaults", fmt=fmt)
        mw_names["__main__"]["Names"]    = Cfg.get_list("ssm.feature.maintenance_window.mainfleet.defaults", fmt=fmt)
        mw_names["__all__"]["Names"]     = Cfg.get_list("ssm.feature.maintenance_window.subfleet.__all__.defaults", fmt=fmt)

        all_mw_names = mw_names["__globaldefault__"]["Names"]
        all_mw_names.extend([ n for n in mw_names["__default__"]["Names"] if n not in all_mw_names])
        all_mw_names.extend([ n for n in mw_names["__main__"]["Names"] if n not in all_mw_names])
        all_mw_names.extend([ n for n in mw_names["__all__"]["Names"] if n not in all_mw_names])

        Cfg.register({
                f"ssm.feature.maintenance_window.subfleet.__all__.force_running":
                    Cfg.get("ssm.feature.maintenance_window.subfleet.{SubfleetName}.force_running"),
                f"ssm.feature.events.ec2.scaling_state_changes.draining.__main__.connection_refused_tcp_ports": 
                    Cfg.get("ssm.feature.events.ec2.scaling_state_changes.draining.connection_refused_tcp_ports")
            })

        for SubfleetName in self.o_ec2.get_subfleet_names():
            fmt["SubfleetName"] = SubfleetName
            mw_names[f"Subfleet.{SubfleetName}"] = {}
            Cfg.register({
                f"ssm.feature.maintenance_window.subfleet.{SubfleetName}.defaults": Cfg.get("ssm.feature.maintenance_window.subfleet.{SubfleetName}.defaults"),
                f"ssm.feature.maintenance_window.subfleet.{SubfleetName}.ec2.schedule.min_instance_count": 
                    Cfg.get("ssm.feature.maintenance_window.subfleet.{SubfleetName}.ec2.schedule.min_instance_count"),
                f"ssm.feature.maintenance_window.subfleet.{SubfleetName}.force_running":
                    Cfg.get("ssm.feature.maintenance_window.subfleet.{SubfleetName}.force_running"),
                f"ssm.feature.events.ec2.scaling_state_changes.draining.{SubfleetName}.connection_refused_tcp_ports": 
                    Cfg.get("ssm.feature.events.ec2.scaling_state_changes.draining.connection_refused_tcp_ports")
            })
            mw_names[f"Subfleet.{SubfleetName}"]["Names"] = Cfg.get_list(f"ssm.feature.maintenance_window.subfleet.{SubfleetName}.defaults", fmt=fmt)
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
                        log.log(log.NOTICE, f"/!\ SSM Maintenance Window '%s' without 'NextExecutionTime'." % wi["Name"])
                    if wi not in mws:
                        mws.append(wi)
            names = names[20:]
        # Make string dates as object dates
        for d in mws:
            if "NextExecutionTime" in d:
                d["NextExecutionTime"] = misc.str2utc(d["NextExecutionTime"])

        # Retrieve Maintenace Window tags with the resourcegroup API
        tagged_mws = self.context["o_state"].get_resources(service="ssm", resource_name="maintenancewindow")
        for tmw in tagged_mws:
            mw_id = tmw["ResourceARN"].split("/")[1]
            mw = next(filter(lambda w: w["WindowId"] == mw_id, mws), None)
            if mw:
                mw["Tags"] = tmw["Tags"]
        valid_mws = []
        for mw in mws:
            mw_id=mw["WindowId"]
            if "Tags" not in mw:
                try:
                    response   = client.list_tags_for_resource(ResourceType='MaintenanceWindow', ResourceId=mw_id)
                    mw["Tags"] = response['TagList'] if 'TagList' in response else []
                except Exception as e:
                    log.error(f"Failed to fetch Tags for MaintenanceWindow '{mw_id}'")
            if ("Tags" not in mw or not len(mw["Tags"])) and mw["Name"] not in mw_names["__globaldefault__"]["Names"]:
                log.warning(f"Please tag SSM Maintenance Window '%s/%s' with 'clonesquad:group-name': '%s'!" %
                        (mw["Name"], mw["WindowId"], self.context["GroupName"]))
                continue
            valid_mws.append(mw)

        # Fetch details about latest execution of valid mws
        #for mw in mws:
        #    mw_id                = mw["WindowId"]
        #    mw["LastExecutions"] = []
        #    paginator = client.get_paginator('describe_maintenance_window_executions')
        #    response_iterator = paginator.paginate(WindowId=mw_id,
        #            Filters = [{
        #                "Key": "ExecutedAfter",
        #                "Values": [ str((now - timedelta(days=1,hours=1)).isoformat("T","seconds").replace("+00:00", "Z")) ]
        #            }])
        #    for r in response_iterator:
        #        mw["LastExecutions"].extend(r["WindowExecutions"])
        #    mw["ActiveExecution"] = (next(filter(lambda ex: 
        #            ex["StartTime"] >= now and now < ex["StartTime"] + timedelta(hours=mw["Duration"]), mw["LastExecutions"]), None))
        #    if mw["ActiveExecution"] is not None:
        #        log.info(f"SSM Maintenance Window {mw_id} is ACTIVE: %s" % json.dumps(mw["ActiveExecution"], default=str))

        
        # All data gathered
        self.maintenance_windows = {
            "Names": mw_names,
            "Windows": valid_mws
        }

        # Update asynchronous results from previously launched commands
        self.update_pending_command_statuses()

        # Update an efficient cache to answer is_maintenance_time() queries
        self.compute_is_maintenance_times()

        # Perform maintenance window house keeping
        self.manage_maintenance_windows()
        if len(mws):
            log.log(log.NOTICE, f"Detected valid SSM maintenance windows: %s" % json.dumps(self.maintenance_windows["Windows"], default=str))
       
        # Hard dependency toward EC2 module. We update the SSM instance initializing states
        self.o_ec2.update_ssm_initializing_states()

    def prepare_ssm(self):
        if not Cfg.get_int("ssm.enable"):
            return

        now       = self.context["now"]
        client    = self.context["ssm.client"]
        # Update instance inventory
        log.debug("describe_instance_information()")
        paginator = client.get_paginator('describe_instance_information')
        response_iterator = paginator.paginate(
            Filters=[
                {
                    'Key': 'tag:clonesquad:group-name',
                    'Values': [self.context["GroupName"]]
                },
            ],
            MaxResults=50)

        instance_infos = []
        for r in response_iterator:
            instance_infos.extend([d for d in r["InstanceInformationList"]])
        self.instance_infos = instance_infos
        log.debug("end - describe_instance_information()")

    def is_feature_enabled(self, feature):
        if not Cfg.get_int("ssm.enable"):
            return False
        return Cfg.get_int(f"ssm.feature.{feature}")

    def is_instance_online(self, i):
        if isinstance(i, str):
            i = self.o_ec2.get_instance_by_id(i)
        instance_id = i["InstanceId"]
        launch_time = i["LaunchTime"]
        return next(filter(lambda i: i["InstanceId"] == instance_id and 
            i["LastPingDateTime"] > launch_time and i["PingStatus"] == "Online", self.instance_infos), None) 

    #####################################################################
    ## SSM RunCommand support
    #####################################################################

    @xray_recorder.capture()
    def update_pending_command_statuses(self):
        client = self.context["ssm.client"]
        self.run_cmd_states = self.o_state.get_state_json("ssm.events.run_commands", default={
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
                            log.log(log.NOTICE, f"Truncated reply from SSM Command Invocation ({cmd_id}/{instance_id}). "
                                "*Cause: SSM exec error? started shell command too verbose? (please limit to 24kBytes max!)")
                        agent_status = "CLONESQUAD-SSM-AGENT-STATUS:"
                        status_msg  = next(filter(lambda s: s.startswith(agent_status), stdout), None)
                        if status_msg is None:
                            status_msg = "ERROR"
                        else:
                            status_msg = status_msg[len(agent_status):]
                        details_msg = list(filter(lambda s: s.startswith("CLONESQUAD-SSM-AGENT-DETAILS:"), stdout))
                        warning_msg = list(filter(lambda s: ":WARNING:" in s, stdout))
                        if len(warning_msg):
                            log.warning(f"Got warning while retrieving SSM RunCommand output for {cmd_id}/{instance_id}/{command}: "
                                    f"{warning_msg}/{details_msg}")

                        result = {
                            "SSMInvocationStatus": status,
                            "Status": status_msg,
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
            if former_results.get(i,{}).get(f"{command};{args}"):
                r[i] = former_results[i][f"{command};{args}"]
        # Coalesce run reqs with similar command
        cmd_to_send = next(filter(lambda c: c["Command"] == command and c["CommandArgs"] == args, self.commands_to_send), None)
        if cmd_to_send:
            for i in non_pending_ids:
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
            platforms = {}
            for i in cmd["InstanceIds"]:
                info = self.is_instance_online(i)
                if info is None:
                    continue
                platform_type = info["PlatformType"]
                pltf          = refs.get(platform_type)
                if pltf is None:
                    log.warning(f"Can't run a command {cmd} on an unsupported platform : {i}/%s" % info["PlatformType"])
                    continue # Unsupported platform
                if platform_type not in platforms:
                    platforms[platform_type] = copy.deepcopy(pltf)
                if i not in platforms[platform_type]["ids"]:
                    platforms[platform_type]["ids"].append(i)

            command = cmd["Command"]
            args    = cmd["CommandArgs"]
            for p in platforms:
                pltf         = platforms[p]
                instance_ids = pltf["ids"]
                if not len(instance_ids):
                    continue
                document     = pltf["document"]
                shell        = pltf["shell"]
                i_ids        = instance_ids
                # Perform string parameter substitutions in the helper script
                shell_input = [l.replace("##Cmd##", command) for l in shell]
                shell_input = [l.replace("##ApiGwUrl##", self.context["InteractAPIGWUrl"]) for l in shell_input]
                if isinstance(args, str):
                    shell_input = [l.replace("##Args##", args) for l in shell_input]
                else:
                    shell_input = [l.replace("##Args##", args["Args"] if "Args" in args else "") for l in shell_input]
                    for s in args:
                        shell_input = [l.replace(f"##{s}##", str(args[s])) for l in shell_input]

                while len(i_ids):
                    log.log(log.NOTICE, f"SSM SendCommand({p}): {command}({args}) to %s." % i_ids[:50])

                    try:
                        response = client.send_command(
                            InstanceIds=i_ids[:50],
                            DocumentName=document,
                            TimeoutSeconds=cmd["Timeout"],
                            Comment=cmd["Comment"],
                            Parameters={
                                'commands': shell_input,
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
                        log.log(log.NOTICE, f"SSM RunCommand (Id:%s) : {command}({args})" % response["Command"]["CommandId"])
                    except Exception as e:
                        # Under rare circumstance, we can receive an Exception while trying to send
                        log.log(log.NOTICE, f"Failed to do SSM SendCommand : {e}, %s" % i_ids[:50])
                    i_ids = i_ids[50:]
        self.o_state.set_state_json("ssm.events.run_commands", self.run_cmd_states, compress=True, TTL=self.ttl)

    def send_events(self, instance_ids, event_class, event_name, event_args, pretty_event_name=None, notification_handler=None):
        if not Cfg.get_int("ssm.enable"):
            return False

        now            = self.context["now"]
        default_struct = {
            "EventName": None,
            "InstanceIdSuccesses": [],
            "InstanceIdsNotified": []
        }
        event_desc = self.o_state.get_state_json(f"ssm.events.class.{event_class}", default=default_struct, TTL=self.ttl)
        if event_name != event_desc["EventName"]:
            event_desc["EventName"]           = event_name
            event_desc["InstanceIdSuccesses"] = []
            event_desc["InstanceIdsNotified"] = []

        # Notify users
        if event_name is not None and notification_handler is not None:
            not_notified_instance_ids = [i for i in instance_ids if i not in event_desc["InstanceIdsNotified"]]
            if len(not_notified_instance_ids):
                R(None, notification_handler, InstanceIds=not_notified_instance_ids, 
                        EventClass=event_class, EventName=event_name, EventArgs=event_args)
                event_desc["InstanceIdsNotified"].extend(not_notified_instance_ids)

        # Send SSM events to instances
        if event_name is None:
            event_desc = default_struct
        elif Cfg.get_int("ssm.feature.events.ec2.maintenance_window_period"):
            ev_ids = [i for i in instance_ids if i not in event_desc["InstanceIdSuccesses"]]
            if len(ev_ids):
                log.log(log.NOTICE, f"Send event {event_class}: {event_name}({event_args}) to {ev_ids}")
                if pretty_event_name is None: 
                    pretty_event_name = "SendEvent"
                comment  = f"CS-{pretty_event_name} (%s)" % self.context["GroupName"]
                r = self.run_command(ev_ids, event_name, args=event_args, comment=comment)
                for i in [i for i in ev_ids if i in r]:
                    if r[i]["Status"] == "SUCCESS":
                        # Keep track that we received a SUCCESS for this instance id to not resend it again later
                        event_desc["InstanceIdSuccesses"].append(i)

        self.o_state.set_state_json(f"ssm.events.class.{event_class}", event_desc, TTL=self.ttl)


    #####################################################################
    ## SSM Maintenance Window support
    #####################################################################

    def _get_maintenance_windows_for_fleet(self, fleet=None):
        global_default_names   = self.maintenance_windows["Names"]["__globaldefault__"]["Names"]
        default_names          = self.maintenance_windows["Names"]["__default__"]["Names"]
        main_default_names     = self.maintenance_windows["Names"]["__main__"]["Names"]
        subfleet_default_names = self.maintenance_windows["Names"]["__all__"]["Names"]
        mws                    = self.maintenance_windows["Windows"]
        names                  = []
        if fleet is None:
            names.extend(main_default_names)
        else:
            names.extend(self.maintenance_windows["Names"][f"Subfleet.{fleet}"]["Names"])
            names.extend(subfleet_default_names)
        names.extend(default_names)
        names.extend(global_default_names)
        # Return a list of maintenance window object ordered by precedence
        return sorted([w for w in mws if w["Name"] in names], key=lambda w: names.index(w["Name"]))

    def exports_metadata_and_backup(self, export_url):
        if not Cfg.get_int("ssm.enable"):
            log.log(log.NOTICE, "SSM support is currently disabled. Set ssm.enable to 1 to enabled it.")
            return
        account_id, region, group_name = (self.context["ACCOUNT_ID"], self.context["AWS_DEFAULT_REGION"], self.context["GroupName"])
        path                           = f"accountid={account_id}/region={region}/groupname={group_name}"

        fleets = [None]
        fleets.extend(self.context["o_ec2"].get_subfleet_names())
        mws    = []
        for fleet in fleets:
            meta = {}
            mw_record= {
                "Fleet": f"{fleet}" if fleet is not None else "__main__",
                "MaintenanceWindows": self._get_maintenance_windows_for_fleet(fleet=fleet),
                "MetadataRecordLastUpdatedAt": self.context["now"],
                "IsMaintenanceTime": self.is_maintenance_time(fleet=fleet, meta=meta)
            }
            mw_record["NextMaintenanceWindowDetails"] = meta
            mw_record = copy.deepcopy(mw_record)
            misc.stringify_timestamps(mw_record)
            mws.append(json.dumps(mw_record, default=str))
        misc.put_url(f"{export_url}/metadata/maintenance-windows/{path}/{account_id}-{region}-maintenance-windows-cs-{group_name}.json", 
                "\n".join(mws))


    def is_maintenance_time(self, fleet=None, meta=None):
        if not self.is_feature_enabled("maintenance_window"):
            return False
        mt = self.is_maintenance_times[fleet]
        if meta is not None:
            meta.update(mt["Meta"])
        return mt["IsMaintenanceTime"]

    def compute_is_maintenance_times(self):
        self.is_maintenance_times = {}
        now         = self.context["now"]
        sa          = max(Cfg.get_duration_secs("ssm.feature.maintenance_window.start_ahead"), 30)
        # We compute a predictive jitter to avoid all subleets starting exactly at the same time
        group_name  = self.context["GroupName"]

        # Step 1) We walk through all matching maintenace windows and compute the real start time taking into account
        #   a jitter to avoid all fleets to start at the same time.
        fleets = [None]
        fleets.extend(self.o_ec2.get_subfleet_names())
        for fleet in fleets:
            meta        = {}
            jitter_salt = int(misc.sha256(f"{group_name}:{fleet}")[:3], 16) / (16 * 16 * 16) * sa
            jitter      = Cfg.get_abs_or_percent("ssm.feature.maintenance_window.start_ahead.max_jitter", 0, jitter_salt)
            start_ahead = timedelta(seconds=(sa-jitter))

            # Step 1) Compute windows start/end and active period flag
            windows = copy.deepcopy(self._get_maintenance_windows_for_fleet(fleet=fleet))
            for w in windows:
                window_id = w["WindowId"]
                state_key = f"ssm.events.maintenance_window.{window_id}"
                if "NextExecutionTime" in w:
                    start_time = w["NextExecutionTime"] - start_ahead 
                    end_time   = w["NextExecutionTime"] + timedelta(hours=int(w["Duration"]))
                    if start_time <= now and now < end_time:
                        self.o_state.set_state(state_key, start_time, TTL=self.ttl)
                    w["_FutureNextExecutionTime"] = w["NextExecutionTime"]
                w["_StartTime"] = self.o_state.get_state_date(state_key, TTL=self.ttl, default=misc.epoch())
                w["_EndTime"]   = w["_StartTime"] + start_ahead + timedelta(hours=w.get("Duration", 1))
                w["_ActivePeriod"]  = w["_StartTime"] <= now and now < w["_EndTime"]
                if not w["_ActivePeriod"] and "NextExecutionTime" in w:
                    w["_StartTime"] = start_time
                    w["_EndTime"]   = end_time

            # Step 2) Select the closest maintenance window
            valid_windows = [w for w in windows if "_EndTime" in w and w["_EndTime"] >= now]
            fleetname     = "Main" if fleet is None else fleet
            next_window   = None
            for w in sorted(valid_windows, key=lambda w: w["_StartTime"]):
                if w["_ActivePeriod"]:
                    meta["MatchingWindow"]        = w
                    meta["MatchingWindowMessage"] = f"Found ACTIVE matching window for fleet {fleetname} : %s" % json.dumps(w, default=str)
                    meta["StartTime"]             = w["_StartTime"]
                    meta["EndTime"]               = w["_EndTime"]
                    self.is_maintenance_times[fleet] = {
                        "IsMaintenanceTime": True,
                        "Meta": meta
                    }
                    break
                if ("_FutureNextExecutionTime" in w and w["_FutureNextExecutionTime"] > now and 
                        (next_window is None or w["_FutureNextExecutionTime"] < next_window["_FutureNextExecutionTime"])):
                    next_window     = w
            if fleet not in self.is_maintenance_times:
                if next_window is not None:
                    w = next_window
                    meta["NextWindowMessage"] = (f"Next SSM Maintenance Window for {fleetname} fleet is '%s / %s in %s "
                        f"(Fleet will start ahead at %s)." % (w["WindowId"], w["Name"], (w["_FutureNextExecutionTime"] - now), 
                            w["_FutureNextExecutionTime"] - start_ahead))
                self.is_maintenance_times[fleet] = {
                    "IsMaintenanceTime": False,
                    "Meta": meta
                }

    def manage_maintenance_windows(self):
        """ Read SSM Maintenance Window information and apply temporary configuration during maintenance period.
        """
        config_tag = "clonesquad:config:"
        def _set_tag(fleet, config, mw):
            min_instance_count = None
            if "Tags" in mw:
                tags = {}
                for t in mw["Tags"]:
                    if t["Key"].startswith(config_tag):
                        tags[t["Key"][len(config_tag):]] = t["Value"]
                if fleet is None:
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
                    if not Cfg.is_builtin_key_exist(t):
                        log.warning(f"On SSM MaintenanceWindow objection %s/%s, tag '{config_tag}.{t}' does not refer "
                            "to an existing configuration key!!" % (mw["WindowId"], mw["Name"]))
                        continue
                    mainfleet_parameter = t.startswith("ec2.schedule.")
                    subfleet_parameter  = t.startswith(f"subfleet.{fleet}.") or t.startswith(f"subfleet.__all__.")
                    if ((fleet is None and mainfleet_parameter) or (fleet is not None and subfleet_parameter) 
                            or (not mainfleet_parameter and not subfleet_parameter)):
                        config[f"override:{t}"] = tags[t]
            return min_instance_count
        config = {}
        meta   = {}
        is_maintenance_time  = self.is_maintenance_time(meta=meta)

        # Send events with SSM and notify users
        instances         = self.o_ec2.get_instances(State="pending,running", main_fleet_only=True)
        instance_ids      = [i["InstanceId"] for i in instances]
        event_name        = "ENTER_MAINTENANCE_WINDOW_PERIOD" if is_maintenance_time else "EXIT_MAINTENANCE_WINDOW_PERIOD"
        pretty_event_name = "EnterMaintenanceWindowPeriod" if is_maintenance_time else "ExitMaintenanceWindowPeriod"
        self.send_events(instance_ids, "maintenance_window.state_change", event_name, {
            }, notification_handler=self.ssm_maintenance_window_event, pretty_event_name=pretty_event_name)

        # Main fleet Maintenance window management
        if not is_maintenance_time:
            if "NextWindowMessage" in meta:
                log.log(log.NOTICE, meta["NextWindowMessage"])
        else:
            log.log(log.NOTICE, f"Main fleet under Active Maintenance Window up to %s : %s / %s" % 
                    (meta["EndTime"], meta["MatchingWindow"]["Name"], meta["MatchingWindow"]["WindowId"]))
            min_instance_count = _set_tag(None, config, meta["MatchingWindow"])
            if min_instance_count is None:
                min_instance_count = Cfg.get("ssm.feature.maintenance_window.mainfleet.ec2.schedule.min_instance_count")
            config["override:ec2.schedule.min_instance_count"] = min_instance_count
            if min_instance_count == "100%":
                config["override:ec2.schedule.desired_instance_count"] = "100%"

        # Subfleet Maintenance window management
        for subfleet in self.o_ec2.get_subfleet_names():
            meta = {}
            is_maintenance_time = self.is_maintenance_time(fleet=subfleet, meta=meta)
            # Send events with SSM and notify users
            instances           = self.o_ec2.get_instances(State="running", instances=self.o_ec2.get_subfleet_instances(subfleet_name=subfleet))
            instance_ids        = [i["InstanceId"] for i in instances]
            event_name          = "ENTER_MAINTENANCE_WINDOW_PERIOD" if is_maintenance_time else "EXIT_MAINTENANCE_WINDOW_PERIOD"
            self.send_events(instance_ids, "maintenance_window.state_change", event_name, {
                }, notification_handler=self.ssm_maintenance_window_event, pretty_event_name=pretty_event_name)

            if not is_maintenance_time:
                if "NextWindowMessage" in meta:
                    log.log(log.NOTICE, meta["NextWindowMessage"])
            else:
                log.log(log.NOTICE, f"Subfleet '{subfleet}' fleet under Active Maintenance Window up to %s : %s / %s" % 
                    (meta["EndTime"], meta["MatchingWindow"]["Name"], meta["MatchingWindow"]["WindowId"]))
                min_instance_count = _set_tag(subfleet, config, meta["MatchingWindow"])
                if min_instance_count is None:
                    min_instance_count = Cfg.get(f"ssm.feature.maintenance_window.subfleet.{subfleet}.ec2.schedule.min_instance_count")
                config[f"override:subfleet.{subfleet}.ec2.schedule.min_instance_count"] = min_instance_count
                if min_instance_count == "100%":
                    config[f"override:subfleet.{subfleet}.ec2.schedule.desired_instance_count"] = "100%"
                if Cfg.get_int("ssm.feature.maintenance_window.subfleet.{SubfleetName}.force_running"):
                    config[f"override:subfleet.{subfleet}.state"] = "running"

        # Register SSM Maintenance Window configuration override
        Cfg.register(config, layer="SSM Maintenance Window override", create_layer_when_needed=True)

    def ssm_maintenance_window_event(self, InstanceIds=None, EventClass=None, EventName=None, EventArgs=None):
        return {}

