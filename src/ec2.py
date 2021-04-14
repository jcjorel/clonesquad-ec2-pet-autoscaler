""" ec2.py

License: MIT

This module provides helper methods manage EC2 instances.

The modules manages:
    * Querying the EC2 API to get various instance statuses
    * Manage EC2 Spot messages.

As all other major modules, the get_prerequisites() method will pre-compute all data needed for all the methods. The overall logic
is that all code outside the get_prerequisites() must work only with data gathered and synthesized in get_prerequisites(). This constraint
ensures easier debugging and more predectible behaviors of various algorithms.

__init__():
    - Registers configuration and CloudWatch attached to the local namespace ("ec2." here).

get_prerequisites():
    - Peform EC2 state discovery (describe_instances(), describe_instance_status(), describe_availability_zones()...)
    - Inject AZ fault if requested by user (or if published by AWS describe_availability_zones() API)
    - Inject Instance status and faults (for debugging purpose)

manage_spot_notification():
    - Intercept and process Spot EC2 messages.

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
import kvtable
import config as Cfg
import debug as Dbg
from notify import record_call as R
from notify import record_call_extended as R_xt

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class EC2:
    @xray_recorder.capture(name="EC2.__init__")
    def __init__(self, context, o_state):
        self.context                 = context
        self.instances               = None
        self.instance_ids            = None
        self.instance_statuses       = None
        self.prereqs_done            = False
        self.o_state                 = o_state
        self.ec2_status_override_url = ""
        self.ec2_status_override     = {}
        self.state_table             = None
        self.scaling_states          = defaultdict(dict)

        Cfg.register({
                 "ec2.describe_instances.max_results" : "500",
                 "ec2.describe_instance_types.enabled": "0",
                 "ec2.az.statusmgt.disable": 0,
                 "ec2.az.unavailable_list,Stable": {
                     "DefaultValue": "",
                     "Format"      : "StringList",
                     "Description" : """List of Availability Zone names (ex: *eu-west-3c*) or AZ Ids (ex: *euw3-az1*).

Typical usage is to force a fleet to consider one or more AZs as unavailable (AZ eviction). The autoscaler will then refuse to schedule
new instances on these AZs. Existing instances in those AZs are left unchanged but on scalein condition will be 
shutdown in priority (see [`ec2.az.evict_instances_when_az_faulty`](#ec2azevict_instances_when_az_faulty) to change this behavior). 
This setting is global and affects instances both in the Main fleet and the Subfleets.

This setting can be used during an AWS LSE (Large Scale Event) to manually define that an AZ is unavailable.

> Note: CloneSquad also uses the EC2.describe_availability_zones() API to discover dynamically LSE events. So, setting directly this key
should not be needed in most cases.

Please notice that, once an AZ is enabled again (either manually or automatically), instance fleet WON'T be rebalanced automatically:
* If Instance bouncing is enabled, the fleet will be progressively rebalanced (convergence time will depend on the instance bouncing setting)
* If instance bouncing is not configured, user can force a rebalancing by switching temporarily the fleet to `100%` during few minutes 
(with [`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count) sets temporarily to `100%`) and switch back to the 
original value.

                     """
                 },
                 "ec2.az.evict_instances_when_az_faulty,Stable": {
                     "DefaultValue": "0",
                     "Format"      : "Bool",
                     "Description" : """Defines if instances running in a AZ with issues must be considered 'unavailable'

By Default, instances running in an AZ reported with issues are left untouched and these instances will only be evicted if
their invidual healthchecks fail or on scalein events.

Settting this parameter to 1 will force Clonesquad to consider all the instances running in faulty AZ as 'unavailable' and so
forcing their immediate replacement in healthy AZs in the region. 
                 """},
                 "ec2.state.default_ttl": "days=1",
                 "ec2.state.error_ttl" : "minutes=5",
                 "ec2.state.status_ttl" : "days=40",
                 "ec2.instance.control.ttl" : "hours=1",
                 "ec2.instance.max_start_instance_at_once": "25",
                 "ec2.instance.max_stop_instance_at_once": "25",
                 "ec2.instance.spot.event.interrupted_at_ttl" : "minutes=10",
                 "ec2.instance.spot.event.rebalance_recommended_at_ttl" : "minutes=20",
                 "ec2.state.error_instance_ids": "",
                 "ec2.state.excluded_instance_ids": {
                     "DefaultValue": "",
                     "Format"      : "List of String",
                     "Description" : """List of instance ids to consider as excluded.

                     One of the 2 ways to exclude existant instances to be managed by CloneSquad, this key is a list of instance ids (ex: 
                     i-077b2ae6988f33de4;i-0564c45bfa5bb6aa5). The other way to exclude instances, is to tag instances with "clonesquad:excluded" key
                     with value 'True'.
                     """
                 },
                 "subfleet.{SubfleetName}.state,Stable": {
                         "DefaultValue": "undefined",
                         "Format": "String",
                         "Description": """Define the status of the subfleet named {SubfleetName}.

Can be one the following values ['`stopped`', '`undefined`', '`running`'].

A subfleet can contain EC2 instances but also RDS and TransferFamilies tagged instances.

Note: **When subfleet name is `__all__`, the key is overriden in all subfleets.**
                 """},
                 "subfleet.{SubfleetName}.ec2.schedule.desired_instance_count,Stable": {
                         "DefaultValue": "100%",
                         "Format": "IntegerOrPercentage",
                         "Description": """Define the number of EC2 instances to start when a subfleet is in a 'running' state.

**Note:** `-1` is an invalid value (and so do not mean 'autoscaling' like in [`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count)).

> This parameter has no effect if [`subfleet.subfleetname.state`](#subfleetsubfleetnamestate) is set to a value different than `running`.
                 """},
                 "subfleet.{SubfleetName}.ec2.schedule.burstable_instance.max_cpu_crediting_instances,Stable": {
                         "DefaultValue": "0%",
                         "Format": "IntegerOrPercentage",
                         "Description": """Define the maximum number of EC2 instances that can be in CPU Crediting state at the same time in the designated subfleet.

Follow the same semantic and usage than [`ec2.schedule.burstable_instance.max_cpu_crediting_instances`](#ec2scheduleburstable_instancemax_cpu_crediting_instances).
                 """},
                 "subfleet.{SubfleetName}.ec2.schedule.min_instance_count,Stable": {
                         "DefaultValue": "0",
                         "Format": "IntegerOrPercentage",
                         "Description": """Define the minimum number of EC2 instances to keep up when a subfleet is in a 'running' state.

> This parameter has no effect if [`subfleet.subfleetname.state`](#subfleetsubfleetnamestate) is set to a value different than `running`.
                 """},
                 "subfleet.{SubfleetName}.ec2.schedule.verticalscale.instance_type_distribution,Stable": {
                         "DefaultValue": ".*,spot;.*",
                         "Format": "MetaStringList",
                         "Description": """Define the vertical policy of the subfleet.

It has a similar semantic than [`ec2.schedule.verticalscale.instance_type_distribution`](#ec2scheduleverticalscaleinstance_type_distribution) except
that it does not support LightHouse instance specifications.

**Due to the default value `.*,spot;.*`, by default, Spot instances are always scheduled first in a subfleet!** This can be changed by the user.

> This parameter has no effect if [`subfleet.subfleetname.state`](#subfleetsubfleetnamestate) is set to a value different than `running`.
                 """},
                 "subfleet.{SubfleetName}.ec2.schedule.metrics.enable,Stable": {
                         "DefaultValue": "1",
                         "Format": "Bool",
                         "Description": """Enable detailed metrics for the subfleet {SubfleetName}.

The following additional metrics are generated:
* Subfleet.EC2.Size,
* Subfleet.EC2.RunningInstances,
* Subfleet.EC2.DrainingInstances.

These metrics are associated to a dimension specifying the subfleet name and are so different from the metrics with similar names from
the autoscaled fleet.

                 """},
                 "ec2.debug.availability_zones_impaired": "",
                 "ec2.instance.status.override_url,Stable": {
                    "DefaultValue": "",
                    "Format"      : "String",
                    "Description" : """Url pointing to a YAML file overriding EC2.describe_instance_status() instance states.

CloneSquad can optionaly load a YAML file containing EC2 instance status override.

The format is a dict of 'InstanceId' containing another dict of metadata:

```yaml
---
i-0ef23917a58368c89:
    status: ok
i-0ad73bbc09cb68f81:
    status: unhealthy
```

The status item can contain any of valid values returned by `EC2.describe_instance_status()["InstanceStatus"]["Status"]`.
The valid values are ["ok", "impaired", "insufficient-data", "not-applicable", "initializing", "unhealthy"].    

**Please notice the special 'unhealthy' value that is a CloneSquad extension:** This value can be injected to force 
an instance to be considered as unhealthy by the scheduler. It can be useful to debug/simulate a failure of a 
specific instance or to inject 'unhealthy' status coming from a non-TargetGroup source (ex: when CloneSquad is used
without any TargetGroup but another external health instance source exists).

                    """
                 }
        })

        self.ttl = Cfg.get_duration_secs("ec2.state.default_ttl")
        self.o_state.register_aggregates([
            {
                "Prefix": "ec2.instance.",
                "Compress": True,
                "DefaultTTL": self.ttl,
                "Exclude" : [
                    "ec2.instance.scaling.state.", 
                    "ec2.instance.spot.event."
                    ]
            }
            ])


    def get_prerequisites(self, only_if_not_already_done=False):
        """ Gather instance status by calling EC2 APIs.
        """
        if only_if_not_already_done and self.prereqs_done:
            return

        self.state_table = self.o_state.get_state_table()
        client           = self.context["ec2.client"]

        # Create the excluded instance id list coming from control state API
        self.instance_control_excluded_ids = self.get_instance_control_excluded_instance_ids()
        if len(self.instance_control_excluded_ids):
            log.info("Instance ids excluded through Instance Control API GW: %s" % self.instance_control_excluded_ids)

        # Retrieve list of instances with appropriate tag
        Filters          = [{'Name': 'tag:clonesquad:group-name', 'Values': [self.context["GroupName"]]}]
        
        log.debug("describe_instances()")
        instances = []
        paginator = client.get_paginator('describe_instances')
        response_iterator = paginator.paginate(Filters=Filters, MaxResults=Cfg.get_int("ec2.describe_instances.max_results"))
        for response in response_iterator:
            for reservation in response["Reservations"]:
                instances.extend(reservation["Instances"])
        log.debug("end - describe_instances()")

        # Filter out instances with inappropriate state
        non_terminated_instances = []
        for i in instances:
            if i["State"]["Name"] not in ["shutting-down", "terminated"]:
                non_terminated_instances.append(i)

        self.instances    = non_terminated_instances
        self.instance_ids = [ i["InstanceId"] for i in self.instances]

        # Enrich instance list with additional data
        for i in self.instances:
            instance_id = i["InstanceId"]
            last_start_attempt = self.get_state_date("ec2.instance.last_start_attempt_date.%s" % instance_id)
            i["_LastStartAttemptTime"] = last_start_attempt if last_start_attempt is not None else i["LaunchTime"]

        # Enrich describe_instances output with instance type details
        if Cfg.get_int("ec2.describe_instance_types.enabled"):
            log.debug("describe_instance_types()")
            self.instance_types = []
            [self.instance_types.append(i["InstanceType"]) for i in self.instances if i["InstanceType"] not in self.instance_types]
            if len(self.instance_types):
                response                   = client.describe_instance_types(InstanceTypes=self.instance_types)
                self.instance_type_details = response["InstanceTypes"]
                for i in self.instances:
                    i["_InstanceType"] = next(filter(lambda it: it["InstanceType"] == i["InstanceType"], self.instance_type_details), None)

        # Get instances status
        instance_statuses = []
        response          = None
        i_ids             = self.instance_ids.copy()
        while len(i_ids):
            log.debug("describe_instance_status()")
            q = { "InstanceIds": i_ids[:100] }
            paginator = client.get_paginator('describe_instance_status')
            response_iterator = paginator.paginate(**q)
            for response in response_iterator:
                instance_statuses.extend(response["InstanceStatuses"])
            response = None
            i_ids    = i_ids[100:]
        self.instance_statuses = instance_statuses

        # Get AZ status
        log.debug("describe_availability_zones()")
        response                = client.describe_availability_zones()
        self.availability_zones = response["AvailabilityZones"]
        if len(self.availability_zones) == 0: raise Exception("Can't have a region with no AZ...")

        self.az_with_issues = []
        if not Cfg.get_int("ec2.az.statusmgt.disable"):
            for az in self.availability_zones:
                if az["State"] in ["impaired", "unavailable"]:
                    self.az_with_issues.append(az) 
                if az["State"] != "available":
                    log.warning("AZ %s(%s) is marked with status '%s' by EC2.describe_availability_zones() API!" % (zone_name, zone_id, zone_state))
        else:
            log.warning("Automatic AZ issues detection through describe_availability_zones() is DISABLED (ec2.az.statusmgt.disable != 0)...")

        # Use these config keys to simulate an AWS Large Scale Event
        all_az_names = [az["ZoneName"] for az in self.availability_zones]
        all_az_ids   = [az["ZoneId"  ] for az in self.availability_zones]
        [ log.warning("ec2.debug.availability_zones_impaired do not match local AZs! '%s'" % a) for a in Cfg.get_list("ec2.debug.availability_zones_impaired", default=[]) if a not in all_az_names and a not in all_az_ids]
        [ log.warning("ec2.az.unavailable_list do not match local AZs! '%s'" % a) for a in Cfg.get_list("ec2.az.unavailable_list", default=[]) if a not in all_az_names and a not in all_az_ids]
        for az in self.availability_zones:
            zone_name  = az["ZoneName"]
            zone_id    = az["ZoneId"]
            zone_state = az["State"]
            if zone_name in Cfg.get_list("ec2.debug.availability_zones_impaired", default=[]): zone_state = "impaired"
            if zone_id   in Cfg.get_list("ec2.debug.availability_zones_impaired", default=[]): zone_state = "impaired"
            if zone_name in Cfg.get_list("ec2.az.unavailable_list", default=[]):               zone_state = "unavailable"
            if zone_id   in Cfg.get_list("ec2.az.unavailable_list", default=[]):               zone_state = "unavailable"
            if zone_state != az["State"] and zone_state in ["impaired", "unavailable"] and az not in self.az_with_issues:
                self.az_with_issues.append(az)
            az["State"] = zone_state
            if zone_state != "available":
                log.warning("AZ %s(%s) is marked with status '%s' by configuration keys!" % (zone_name, zone_id, zone_state))

        # We need to register dynamically subfleet configuration keys to avoid a 'key unknown' warning 
        #   when the user is going to set it
        subfleet_names = ["__all__"]
        subfleet_names.extend(self.get_subfleet_names())
        for subfleet in subfleet_names:
            for k in Cfg.keys():
                key = k.replace("{SubfleetName}", subfleet)
                if k.startswith("subfleet.{SubfleetName}.") and not Cfg.is_builtin_key_exist(key):
                    Cfg.register({ f"{key},Stable" : Cfg.get(k) if subfleet != "__all__" else None })
        if len(subfleet_names) > 1:
            log.log(log.NOTICE, "Detected following subfleet names across EC2 resources: %s" % subfleet_names)

        # Load EC2 status override URL content
        self.ec2_status_override_url = Cfg.get("ec2.instance.status.override_url")
        if self.ec2_status_override_url is not None and self.ec2_status_override_url != "":
            log.debug("Load ec2_status_override_url %s" % ec2_status_override_url)
            try:
                content = misc.get_url(self.ec2_status_override_url)
                self.ec2_status_override = yaml.safe_load(str(content, "utf-8"))
            except Exception as e:
                log.warning("Failed to load 'ec2.instance.status.override_url' YAML file '%s' : %s" % (self.ec2_status_override_url, e))

        # Pre-compute scaling states for instance tp be fast later
        log.debug("compute_scaling_states()")
        self.compute_scaling_states()

        self.prereqs_done = True

    def register_state_aggregates(self, aggregates):
        self.o_state.register_aggregates(aggregates)

    def get_instance_statuses(self):
        """ Return the result of describe_instance_status()
        """
        return self.instance_statuses

    def update_ssm_initializing_states(self):
        o_ssm = self.context["o_ssm"]
        if not o_ssm.is_feature_enabled("events.ec2.instance_ready_for_operation"):
            return

        now                             = self.context["now"]
        self.ssm_initializing_instances = {}
        instances                       = self.get_instances(State="pending,running", ScalingState="-draining")
        for i in instances:
            instance_id     = i["InstanceId"]
            if self.is_instance_excluded(instance_id):
                continue
            launch_time     = i["LaunchTime"]
            last_start_date = self.get_state_date(f"ec2.instance.ssm.ready_for_operation.start_date.{instance_id}", TTL=self.ttl)
            if last_start_date is None or last_start_date < launch_time:
                self.set_state(f"ec2.instance.ssm.ready_for_operation.start_date.{instance_id}", now)
                last_start_date = now
            last_ready_date = self.get_state_date(f"ec2.instance.ssm.ready_for_operation.ok_date.{instance_id}", TTL=self.ttl)
            last_ready_date = last_ready_date if last_ready_date is None or last_ready_date > last_start_date else None
            if last_ready_date is not None:
                continue
            # Need status refresh
            readyness = o_ssm.run_command([instance_id], "INSTANCE_READY_FOR_OPERATION", 
                    comment="CS-InstanceReadyForOperation (%s)" % self.context["GroupName"])
            status    = readyness.get(instance_id, {}).get("Status")
            if status == "SUCCESS":
                self.set_state(f"ec2.instance.ssm.ready_for_operation.ok_date.{instance_id}", now)
                continue

            unhealthy = False
            if (now - last_start_date) > timedelta(seconds=Cfg.get_duration_secs("ssm.feature.events.ec2.instance_ready_for_operation.max_initializing_time")):
                    log.warning(f"Instance {instance_id} spent too much time in initializing state. Report it as unhealthy...")
                    unhealthy = True
            self.ssm_initializing_instances[instance_id] = {
                "LastStartDate": last_start_date,
                "LastReadyDate": last_ready_date,
                "Unhealthy": unhealthy
                }

    INSTANCE_STATES = ["ok", "impaired", "insufficient-data", "not-applicable", "initializing", "unhealthy", "az_evicted"]
    def is_instance_state(self, instance_id, state):
        """ Perform test if data returned by describe_instance_status().

        This method returns is the specified is in one of the state listed in 'state' variable.
        The fiels instance["InstanceState"]["Name"] is compared.
        Note: a special "az_evicted" value is understood that is not port of the values returned by describe_instance_status():
            This special value is used to report that the instance is faulty because running in a faulty AZ.

        :param instance_id: The instance id to test
        :param state: A list of state value to test
        """
        now = self.context["now"]

        # Retrieve the instance structure based on the id
        i = next(filter(lambda i: i["InstanceId"] == instance_id, self.instance_statuses), None)
        if i is None:
            return False

        # Check for "az_evicted" synthetic status
        if Cfg.get_int("ec2.az.evict_instances_when_az_faulty") and "az_evicted" in state:
            az = self.get_instance_by_id(instance_id)["Placement"]["AvailabilityZone"]
            if az in self.get_azs_with_issues():
                return True

        # Check if the status of this instance Id is overriden with an external YAML file
        if i["InstanceState"]["Name"] in ["pending", "running"] and instance_id in self.ec2_status_override:
            override = self.ec2_status_override[instance_id]
            if "status" in override:
                override_status = override["status"]
                if override_status not in INSTANCE_STATES:
                    log.warning("Status override for instance '%s' (defined in %s) has an unmanaged status (%s) !" % 
                            (instance_id, self.ec2_status_override_url, override_status))
                else:
                    return override_status in state

        o_ssm = self.context["o_ssm"]
        if o_ssm.is_feature_enabled("events.ec2.instance_ready_for_operation") and instance_id in self.ssm_initializing_instances:
            status = self.ssm_initializing_instances[instance_id]
            if "initializing" in state:
                if status["LastReadyDate"] is not None:
                    state.remove("initializing")
                    return i["InstanceStatus"]["Status"] in state
            if "unhealthy" in state and status["Unhealthy"]:
                    return True
        
        return i["InstanceStatus"]["Status"] in state 

    def get_azs_with_issues(self):
        """ Return a list of AZ 'zone name' (ex: eu-west-1a) that have issues.
        """
        return [ az["ZoneName"] for az in self.az_with_issues ]

    def is_instance_excluded(self, instance_id):
        """ Test if an instance is excluded.
        """
        excluded_instances  = Cfg.get_list("ec2.state.excluded_instance_ids", default=[])
        instance            = self.get_instance_by_id(instance_id) if isinstance(instance_id, str) else instance_id
        instance_id         = instance["InstanceId"]
        if ((instance and self.instance_has_tag(instance, "clonesquad:excluded", value=["1", "True", "true"]))
            or instance_id in excluded_instances 
            or instance_id in self.instance_control_excluded_ids):
            return True
        return False

    def get_subfleet_instances(self, subfleet_name=None, with_excluded_instances=False):
        """ Return a list of instance structure that are part the specified subfleet.

        :param subfleet_name: If 'None', return all subfleets in all subfleets; if set, filter on the subfleet name specified.
        :return A list instance structures
        """
        value     = [subfleet_name] if subfleet_name is not None else None
        instances = self.filter_instance_list_by_tag(self.instances, "clonesquad:subfleet-name", value)
        if not with_excluded_instances:
            instances = self.filter_instance_list_by_tag(instances, "-clonesquad:excluded", ["True", "true"])
            instances = [i for i in instances if i["InstanceId"] not in self.instance_control_excluded_ids]
        return instances

    def get_subfleet_names(self):
        """ Return the list of all active subfleets.
        """
        instances = self.get_subfleet_instances(with_excluded_instances=True)
        names     = []
        for i in instances:
            tags = self.get_instance_tags(i)
            [names.append(tags[k]) for k in tags if k == "clonesquad:subfleet-name" and tags[k] not in names]
        return names

    def get_subfleet_name_for_instance(self, i):
        """ Return the name of subfleet that the instance is part of or None is not part of a subfleet.
        """
        if isinstance(i, str):
            i = self.get_instance_by_id(i)
        tags = self.get_instance_tags(i)
        return tags["clonesquad:subfleet-name"] if "clonesquad:subfleet-name" in tags else None

    def is_subfleet_instance(self, instance_id, subfleet_name=None):
        """ Return 'True' if specified instance is part of a subfleet. 
        If 'subfleet_name' is specified, it also check that the instance is part of the specified subfleet.
        """
        instances    = self.get_subfleet_instances(subfleet_name=subfleet_name)
        instance_ids = [i["InstanceId"] for i in instances]
        return instance_id in instance_ids

    def get_timesorted_instances(self, instances=None):
        """ Return a sortied list of instance structure.

        The list is sorted with from the oldest to newest last start attempt. (As a consequence, it makes sure that
        an instance that failed to start for any reason won't the one that will be attempted at next scheduling).
        """
        if instances is None: instances=self.instances
        # Sort instance list starting from the oldest launch to the newest
        return sorted(instances, key=lambda i: i["_LastStartAttemptTime"])

    def get_instances(self, instances=None, State=None, ScalingState=None, main_fleet_only=False, details=None, max_results=-1, azs_filtered_out=None):
        """ Return a list of instance structures based on specified criteria.

        This method is a critical one used in all scheduling algorithms.
        It sorts and filters this way:
            * Sort all instances from the oldest running to the youngest running.
            * Filter based on 'State' that represents the instance state (["pending", "running", "stopped"...] - see describe_instances())
            * Filter based on 'ScalingState' that represents an algorithm PoV state ["draining", "error", "bounced"]:
                - "draining": Instance with this scaling state will be soon retired after graceful shutdown sequence (draining period...)
                - "error": Instance with this scaling state failed to perform a critical operation like start_instance() or stop_instance()
                    Instance is this scaling state are usually blacklisted for 5 minutes to pass a possible transient state.
                - "bounced": Instance with scaling state will soon be bounced by the bouncing algorithm (too old)

        TODO: Currently, the way it is implemented is highly inefficient and very CPU intensice. TO REWRITE FROM SCRATCH.

        :return A list of instance structure.
        """
        if details is None: details = {}
        details.update({
            "state" : {"filtered-in": [],
                "filtered-out": []},
            "scalingstate" : {"filtered-in": [],
                "filtered-out": []},
            })

        ref_instances = self.instances if instances is None else instances
        if main_fleet_only: # Filter out all subfleet instances
            ref_instances = [i for i in ref_instances if self.get_subfleet_name_for_instance(i) is None]
        instances = []
        for instance in ref_instances:
           state_test        = self._match_instance(details["state"], instance, State, 
                   lambda i, value: i["State"]["Name"] in value.split(","))
           scalingstate_test = self._match_instance(details["scalingstate"], instance, ScalingState, 
                   lambda i, value: self.get_scaling_state(i["InstanceId"], do_not_return_excluded=True) in value.split(",") or self.get_scaling_state(i["InstanceId"]) in value.split(","))
           if state_test and scalingstate_test:
               instances.append(instance)

        # Instance list always sorted from the oldest to the newest
        sorted_instances = self.get_timesorted_instances(instances=instances)
        
        # Remove instances from specified AZs
        if azs_filtered_out is not None:
            # Remove instance candidates from disabled AZs
            sorted_instances = [i for i in filter(lambda i: i["Placement"]["AvailabilityZone"] not in azs_filtered_out, sorted_instances) ]

        if max_results >= 0:
            return sorted_instances[:max_results]
        return sorted_instances

    def start_instances(self, instance_ids_to_start, max_started_instances=-1):
        """ Call EC2 start_instance() is a smart way...

        This critical method is responsible to implement safest logic to start instances when needed.
        It doesn't simply call the EC2 start_instances() API but manages corner cases in the most efficient way possible.

        Heuristics:
            * It is recommended to pass as many startable instance ids as arguments and a defined max_startable_instances value. It allows
                the method to manage an instance failure by trying to launch the next one in the list.
            * The start_instances() API is know to fail at once when a single instance fails to start. This method detects this case and 
                try to start one-by-one all instance required to not be stuck in always-failing loop.
            * This method manages a special case linked to Sport instance start. IT is very common that a just stopped Spot instance can not
                restarted immediatly. The heuristic detects this case and assume that it is a transient condition and not worth to notice 
                the user about this event.

        :param instance_ids_to_start: A list of starteable instance ids
        :param max_startable_instances: Maximum number of succesfully instances to start.
        """
        # Remember when we tried to start all these instances. Used to detect instances with issues
        #    by placing them at end of get_instances() generated list
        if instance_ids_to_start is None or len(instance_ids_to_start) == 0:
            log.log(log.NOTICE, "No instance to start...")
            return 
        now = self.context["now"]

        max_startable_instances = max_started_instances if max_started_instances != -1 else len(instance_ids_to_start)

        def _check_response(need_longterm_record, response, ex):
            nonlocal max_startable_instances
            log.debug(Dbg.pprint(response))
            if ex is None:
                metadata = response["ResponseMetadata"]
                if metadata["HTTPStatusCode"] == 200:
                    s = response["StartingInstances"]
                    for r in s:
                        instance_id    = r["InstanceId"]
                        previous_state = r["PreviousState"]
                        current_state  = r["CurrentState"]
                        if current_state["Name"] in ["pending", "running"]:
                            self.set_scaling_state(instance_id, "") # Reset scaling state
                            self.set_state("ec2.instance.last_start_date.%s" % instance_id, now, TTL=self.ttl)
                            max_startable_instances -= 1
                            # Update statuses
                            instance = self.get_instance_by_id(instance_id)
                            instance["State"]["Code"] = 0
                            instance["State"]["Name"] = "pending"
                        else:
                            log.error("Failed to start instance '%s'! Blacklist it for a while... (pre/current status=%s/%s)" %
                                    (instance_id, previous_state["Name"], current_state["Name"]))
                            self.set_scaling_state(instance_id, "error")
                            R(None, self.instance_in_error, Operation="start", InstanceId=instance_id, 
                                    PreviousState=previous_state["Name"], CurrentState=current_state["Name"])
                else:
                    log.error(f"Failed to call start_instances: {response}")

            need_shortterm_record = True
            if ex is not None:
                # If we received an IncorrectSpotRequestState exception, we do not create short and long term record (=do not notify 
                #   user) as it could happen when a Spot instance has recently been shutdown.
                try:
                    if ex.response['Error']['Code'] == 'IncorrectSpotRequestState':
                        log.log(log.NOTICE, "Failed to start a Spot instance (IncorrectSpotRequestState) among these instances to "
                                f"start {instance_ids_to_start}. It could happen when a Spot has been recently stopped. Will try again next time...")
                        need_shortterm_record  = False
                        need_longterm_record   = False
                    if ex.response['Error']['Code'] == 'InsufficientInstanceCapacity':
                        log.warning(f"Failed to start instance due to 'InsufficientInstanceCapacity' error: {ex}")
                        need_shortterm_record  = False
                        need_longterm_record   = False
                except:
                    pass

            # Instruct the notify handler about what to do regarding record creation
            return { 
                "need_shortterm_record": need_shortterm_record,
                "need_longterm_record": need_longterm_record}


        client = self.context["ec2.client"]
        ids    = instance_ids_to_start
        while len(ids):
            max_start = max(0, min(max_startable_instances, Cfg.get_int("ec2.instance.max_start_instance_at_once")))
            if max_start == 0:
                break
            to_start  = ids[:max_start]
            ids       = ids[max_start:]

            for i in to_start:
                self.set_state("ec2.instance.last_start_attempt_date.%s" % i, now)

            log.info("Starting instances %s..." % to_start)
            response = None
            try:
                response = R_xt(_check_response, lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                    client.start_instances, InstanceIds=to_start
                )
            except Exception as e:
                log.log(log.NOTICE, f"Got Exception while trying to start instance(s) '{to_start}' : {e}. Trying again one-by-one...")
                for i in to_start:
                    try:
                        response = R_xt(_check_response, lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                            client.start_instances, InstanceIds=[i]
                        )
                    except ClientError as e:
                        if e.response['Error']['Code'] != 'IncorrectSpotRequestState':
                            log.warning("Got Exception while trying to start instance '%s' : %s" % (i, e))
                            self.set_scaling_state(i, "error")


    def stop_instances(self, instance_ids_to_stop):
        """ Stop instances the smart way...

        This method is paranoid in the way to stop instances. It tries first to stop them at once but it fails it falls back to
        one-by-one stop_instances() call. It is designed to ensure that a single instance condition blocks any instance stop.

        :param instance_ids_to_stop: A list of instance id to stop
        """
        now      = self.context["now"]
        client   = self.context["ec2.client"]
        ids      = instance_ids_to_stop
        max_stop = Cfg.get_int("ec2.instance.max_stop_instance_at_once")
        while len(ids):
            to_stop = ids[:max_stop]
            ids     = ids[max_stop:]
            try:
                response = R(lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                        client.stop_instances, InstanceIds=to_stop
                   )
                if response is not None and "StoppingInstances" in response:
                    for i in response["StoppingInstances"]:
                        instance_id = i["InstanceId"]
                        self.set_scaling_state(instance_id, "")
                        self.set_state("ec2.schedule.instance.last_stop_date.%s" % instance_id, now) 
                        # Update the statuses 
                        instance = self.get_instance_by_id(instance_id)
                        instance["State"]["Code"] = 64
                        instance["State"]["Name"] = "stopping"
                log.debug(response)
            except Exception as e:
                log.warning("Failed to stop_instance(s) '%s' : %s" % (to_stop, e))
                # Failed to stop all instances at once. Try one by one...
                for i in to_stop:
                    try:
                        response = R(lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                                client.stop_instances, InstanceIds=[i]
                           )
                        if response is not None and "StoppingInstances" in response:
                            for i in response["StoppingInstances"]:
                                instance_id = i["InstanceId"]
                                self.set_scaling_state(instance_id, "")
                                self.set_state("ec2.schedule.instance.last_stop_date.%s" % instance_id, now)
                                # Update the statuses 
                                instance = self.get_instance_by_id(instance_id)
                                instance["State"]["Code"] = 64
                                instance["State"]["Name"] = "stopping"
                        log.debug(response)
                    except Exception as e:
                        log.warning("Failed to stop_instance '%s' : %s" % (i, e))

    def instance_last_stop_date(self, instance_id, default=misc.epoch()):
        return self.get_state_date("ec2.schedule.instance.last_stop_date.%s" % instance_id, default=default)

    def instance_in_error(self, Operation=None, InstanceId=None, PreviousState=None, CurrentState=None):
        """ Template method for user Event generation. 
        """
        return {}

    def sort_by_prefered_azs(self, instances, prefered_azs=None, prefered_before=True):
        """ Return a list of instance sorted by prefered_azs.

        It used by algorithms to manage AZ eviction by putting instances in a faulty AZ in front or at end of this list 
        depending on what is needed by the algorithm.

        """
        if prefered_azs is None: return instances

        sorted_instances = []
        for i in instances:
            if prefered_before and i["Placement"]["AvailabilityZone"] in prefered_azs:
                sorted_instances.append(i)

        for i in instances:
            if i["Placement"]["AvailabilityZone"] not in prefered_azs:
                sorted_instances.append(i)

        for i in instances:
            if not prefered_before and i["Placement"]["AvailabilityZone"] in prefered_azs:
                sorted_instances.append(i)

        return sorted_instances

    def sort_by_prefered_instance_ids(self, instances, prefered_ids=None, prefered_before=True):
        """ Return a sorted list of instance structures based on 'prefered_ids'.

        Used by scaling algorithms to put in high or low priority a set of designed instance ids.

        """
        if prefered_ids is None: return instances

        sorted_instances = []
        for i in instances:
            if prefered_before and i["InstanceId"] in prefered_ids:
                sorted_instances.append(i)

        for i in instances:
            if i["InstanceId"] not in prefered_ids:
                sorted_instances.append(i)

        for i in instances:
            if not prefered_before and i["InstanceId"] in prefered_ids:
                sorted_instances.append(i)

        return sorted_instances


    def sort_by_balanced_az(self, candidate_instances, ref_instances, smallest_to_biggest_az=True, excluded_instance_ids=None):
        """ Sort the supplied candidate instance list in a way that keeps the AZ balanced.

        This is a critical method that scaling algorithm call to ensure AZs are always kept balanced the best possible.

        :param candidate_instances:     The list of instance structures to sort
        :param ref_instances:           The list of instance already running
        :param smallest_to_biggest_az:  Define if the order of sorting.
        :param excluded_instance_ids:   A list of instance ids to ignore in the AZ weighting.
        """
        candidate_instances = candidate_instances.copy()
        ref_azs = {} 
        for i in candidate_instances:
            az = i["Placement"]["AvailabilityZone"]
            ref_azs[az] = 0 if "az" not in ref_azs else 0
        for i in ref_instances:
            if excluded_instance_ids is not None and i["InstanceId"] in excluded_instance_ids:
                continue
            az = i["Placement"]["AvailabilityZone"]
            ref_azs[az] = 1 if az not in ref_azs else ref_azs[az] + 1

        ref_azs_list = []
        for az in ref_azs.keys():
            ref_azs_list.append({
                    "AZ": az,
                    "Count": ref_azs[az]
                })

        sort_direction = 1 if smallest_to_biggest_az else -1
                
        optimized_instances = []
        while len(candidate_instances) > 0:
            # Sort based on number of instances per AZ 
            ref_azs_list.sort(key=lambda x: sort_direction * x["Count"])

            found_candidate = False
            for prefered_az in ref_azs_list:
                for i in candidate_instances:
                    az = i["Placement"]["AvailabilityZone"]
                    if az == prefered_az["AZ"]:
                        prefered_az["Count"] += sort_direction
                        optimized_instances.append(i)
                        candidate_instances.remove(i)
                        found_candidate = True
                        break
                if found_candidate:
                    break
        return optimized_instances

    def get_instance_tags(self, instance, default=None):
        """ Return instance tags as simple dict.
        """
        if instance is None:
            return default
        tags = {}
        for t in instance["Tags"]:
            tags[t["Key"]] = t["Value"]
        return tags


    def instance_has_tag(self, instance, tag, value=None):
        """ Test if an instance has the specified tag and, if defined, the specified value.
        """
        if instance is None:
            return None
        for t in instance["Tags"]:
            if t["Key"] != tag:
                continue
            if value is None:
                return t["Value"]
            return t["Value"] if t["Value"] in value else None 
        return None

    def filter_spot_instances(self, instances, EventType="+rebalance_recommended,interrupted,other_states", 
            filter_out_instance_types=None, filter_in_instance_types=None, match_only_spot=False, merge_matching_spot_first=False):
        """ Filter the supplied 'instances' list related to Spot one.

        :param instances:                   The instance list to filter
        :param EventType:                   A query string that matchs (+) or excluded (-) Spot instance based on 
            status 'rebalance_recommended' and 'interrupted' 
            TODO: Remove 'other_states' as deprecated
        :param filter_out_instance_types:   A list of tuple(InstanceType, AZ) to filter out
        :param filter_in_instance_types:    A list of tuples(InstanceType, AZ) to filter in
        :param match_only_spot:             Excluded of non-Spot instance of the output list
        :param merge_matching_spot_first:   If set to True, Spot instance are at front of the list and all non Spot are appended to the output list.
        :return A list of instances matching the specified selectors.
        """
        now     = self.context["now"]
        res     = []
        exclude = EventType.startswith("-")
        types   = EventType[1:].split(",")
        for i in instances:
            instance_id    = i["InstanceId"]
            instance_type  = i["InstanceType"]
            instance_az    = i["Placement"]["AvailabilityZone"]
            is_spot        = "SpotInstanceRequestId" in i
            if match_only_spot and not is_spot:
                continue
            if is_spot:
                if filter_in_instance_types is not None:
                    m_in  = next(filter(lambda t: t["AvailabilityZone"] == instance_az and t["InstanceType"] == instance_type, filter_in_instance_types), None)
                    if m_in is None: continue
                if filter_out_instance_types is not None:
                    m_out = next(filter(lambda t: t["AvailabilityZone"] == instance_az and t["InstanceType"] == instance_type, filter_out_instance_types), None)
                    if m_out is not None: continue

            for t in ["rebalance_recommended", "interrupted"]:
              if t not in types:
                  continue
              event_at = EC2.get_spot_event(self.context, instance_id, t)
              if event_at is not None:
                    if exclude: continue
                    if i not in res: res.append(i)
                    continue
            if "other_states" in types:
                if exclude: continue
                if i not in res: res.append(i)
                continue

        if merge_matching_spot_first:
            merged       = res
            instance_ids = [ i["InstanceId"] for i in res ]
            [ merged.append(i) for i in instances if i["InstanceId"] not in instance_ids ]
            return merged

        return res

    def filter_out_excluded_instances(self, instances):
        return [i for i in instances if not self.is_instance_excluded(i)]

    def filter_instance_recently_stopped(self, instances, min_age, filter_only_spot=True):
        """ Filter out recently stopped instance.
        TODO: Remove this method as it was used by scaling algorithms to exclude Spot instances recently stopped but as start_instances()
        is now able to manage this smartly so no more need to filter this way...
        """
        now = self.context["now"]
        res = []
        for i in instances:
            instance_id    = i["InstanceId"]
            last_stop_date = self.instance_last_stop_date(instance_id)
            if filter_only_spot and "SpotInstanceRequestId" not in i: 
                res.append(i)
                continue
            if last_stop_date is None or (now - last_stop_date).total_seconds() > min_age:
                res.append(i)
        return res

    def filter_instance_list_by_tag(self, instances, key, value=None):
        """ Perform sort-in/sort-out operation of the supplied instance list based on tags.

        :param instances:   The instance list to filter
        :param key:         The instance key to search prepend with '+' for filter-in and '-' for filter-out
        :param value:       When set, the filter looks not only for tag presence but also tag value.
        :return             A list of filtered instances
        """
        exclude = key.startswith("-")
        if exclude: key = key[1:]

        i_s = []
        for i in instances:
            has_tag = self.instance_has_tag(i, key, value)
            if (not exclude and has_tag) or (exclude and not has_tag):
                i_s.append(i)
        return i_s

    def _match_instance(self, details, instance, value, default_func):
        exclude = False
        filter_func = value
        if type(value) is str:
            if value.startswith("-"): # Find NOT matching value
                exclude = True
                value = value[1:]
            filter_func = default_func
        else:
            value = None
        r = exclude if filter_func is not None and not filter_func(instance, value) else not exclude
        if r:
            details["filtered-in"].append(instance)
        else:
            details["filtered-out"].append(instance)
        return r

    def get_running_instances(self, details=None):
        return self.get_instances(State="pending,running", ScalingState="-error,excluded", details=details)

    def get_burstable_instances(self, State="running", ScalingState="-error,excluded"):
        return [ i for i in self.get_instances(State=State, ScalingState=ScalingState) if i["InstanceType"].startswith("t")]

    def get_non_burstable_instances(self, State="running", ScalingState="-error,excluded"):
        return [ i for i in self.get_instances(State="running", ScalingState="-error,excluded") if not i["InstanceType"].startswith("t")]

    def get_instance_ids(self, instances, max_results=-1):
        ids = []
        for i in instances:
            ids.append(i["InstanceId"])
        if max_results >= 0:
            return ids[:max_results]
        return ids

    def get_instance_by_id(self, id):
        return next(filter(lambda instance: instance['InstanceId'] == id, self.instances), None)

    def get_cpu_creditbalance(self, instance):
        """ Return the CPU Credit balance for the specified instance structure.

        Return -1 if not a burstable intance or CPU Credit is not yet known.
        """
        debug_state_key           = "ec2.debug.instance.%s.cpu_credit_balance" % instance["InstanceId"]
        forced_cpu_credit_balance = self.get_state(debug_state_key)
        if forced_cpu_credit_balance is not None:
            try:
                log.warn("Forcing CPU Credit Balance with state key '%s'!" % debug_state_key)
                return int(forced_cpu_credit_balance)
            except Exception as e:
                log.exception("Failed to convert '%s' as a int()!" % debug_state_key)

        if "_Metrics" not in instance:
            return -1
        metrics = instance["_Metrics"]
        if "CPUCreditBalance" not in metrics:
            return -1
        metric  = metrics["CPUCreditBalance"]
        if len(metric["Values"]): 
            return metric["Values"][0]
        return -1

    def get_all_scaling_states(self):
        """ Return a dict of scaling instance states and associated list of instance id.
        """
        r = defaultdict(list)
        for i in self.get_instances():
            instance_id = i["InstanceId"]
            state = self.get_scaling_state(instance_id, default="unknown")
            r[state].append(instance_id)
        return dict(r)

    def compute_scaling_states(self, instance_id=None):
        """ Compute an optimized lookup structure of scaling instance state.
        """
        def _update_scaling_state(i):
            instance_id  = i["InstanceId"]
            state        = self.scaling_states[instance_id]
            key          = f"ec2.instance.scaling.state.{instance_id}"
            state["raw"] = self.get_state(key, default=None)
            state["state"]             = state["raw"]
            state["state_no_excluded"] = state["raw"]
            if (self.is_instance_excluded(i) or self.is_subfleet_instance(instance_id)):
                state["state"] = "excluded"
            # Force error state for some VM (debug usage)
            if instance_id in error_instance_ids:
                state["state_no_excluded"] = "error"
                state["state"]             = "error"

        error_instance_ids  = Cfg.get_list("ec2.state.error_instance_ids", default=[]) 
        if instance_id is not None:
            _update_scaling_state(self.get_instance_by_id(instance_id))
        else:
            self.scaling_states = defaultdict(dict)
            for i in self.get_instances():
                _update_scaling_state(i)

    def get_scaling_state(self, instance_id, default=None, meta=None, default_date=None, do_not_return_excluded=False, raw=False):
        """ Return the scaling state for specified instance.

        """
        if meta is not None:
            newest_action_date = None
            i                  = self.get_instance_by_id(instance_id)
            last_start_date    = i["LaunchTime"] if "LaunchTime" in i else None
            for action in ["draining", "error", "bounced"]:
                date = misc.str2utc(self.get_state(f"ec2.instance.scaling.last_{action}_date.{instance_id}", TTL=self.ttl))
                meta[f"last_{action}_date"] = date if (date is None or last_start_date is None or date >= last_start_date) else None
                if date is not None and (newest_action_date is None or newest_action_date < date):
                    newest_action_date = date
            meta["last_action_date"] = newest_action_date

        state = self.scaling_states.get(instance_id, {"raw": default, "state_no_excluded": default, "state": default})
        if raw:
            return state["raw"] if state["raw"] is not None else default
        if do_not_return_excluded:
            return state["state_no_excluded"] if state["state_no_excluded"] is not None else default
        return state["state"] if state["state"] is not None else default

    def set_scaling_state(self, instance_id, value, ttl=None, meta=None, default_date=None, force=False):
        if ttl is None: ttl = self.ttl
        if default_date is None: default_date = self.context["now"]

        if value != "":
            meta           = {} if meta is None else meta
            previous_value = self.get_scaling_state(instance_id, meta=meta, do_not_return_excluded=True)
            date           = meta["last_action_date"] if not force and previous_value == value else default_date
            meta[f"last_{value}_date"] = date
            meta[f"last_action_date"] = date
            self.set_state(f"ec2.instance.scaling.last_{value}_date.{instance_id}", date, TTL=ttl)
        else:
            # Remove all state keys
            for action in ["action", "draining", "error", "bounced"]:
                self.set_state(f"ec2.instance.scaling.last_{action}_date.{instance_id}", "", TTL=ttl)
        self.set_state(f"ec2.instance.scaling.state.{instance_id}", value, TTL=ttl)
        # Update the cache
        self.compute_scaling_states(instance_id=instance_id)

    def list_states(self, prefix="ec2.instance.scaling_state.", not_matching_instances=None):
        r = self.state_table.get_keys(prefix) 
        if not_matching_instances is not None:
            filtered_r = r.copy()
            for key in r:
                m = re.search("^ec2.instance.scaling_state.(i-[a-z0-9]+)", key)
                if len(m.groups()) == 0:
                    continue
                instance_id = m.group(1)
                if self.get_instance_by_id(instance_id) is not None:
                    filtered_r.remove(key)
            return filtered_r
        return r

    def get_state(self, key, default=None, direct=False, TTL=None):
        return self.o_state.get_state(key, default=default, direct=direct, TTL=TTL)

    def get_state_int(self, key, default=0, direct=False, TTL=None):
        return self.o_state.get_state_int(key, default=default, direct=direct, TTL=TTL)

    def get_state_json(self, key, default=None, direct=False, TTL=None):
        return self.o_state.get_state_json(key, default=default, direct=direct, TTL=TTL)

    def set_state_json(self, key, value, compress=True, TTL=None):
        if TTL is None: TTL = self.ttl
        self.o_state.set_state_json(key, value, compress=compress, TTL=TTL)

    def get_state_date(self, key, default=None, direct=False, TTL=None):
        return self.o_state.get_state_date(key, default=default, direct=direct, TTL=TTL)

    def set_state(self, key, value, direct=False, TTL=None):
        if TTL is None: TTL = self.ttl
        self.o_state.set_state(key, value, direct=direct, TTL=TTL)

    ### 
    # State management with temporal integration
    ###

    def get_integrated_float_state(self, key, integration_period, default=0.0, favor_max_value=True):
        now       = self.context["now"]
        recs      = self._decode_integrate_float(key, integration_period)
        seconds   = 0
        value     = 0.0
        recs_len  = len(recs)

        if recs_len == 0: return default
        if recs_len == 1: return recs[0][2]

        max_value = recs[0][2]

        prev_time = recs[0][1]
        for i in range(0,recs_len-1):
            s, d, v   = recs[i]
            next_time = recs[i+1][1]
            delta     = (prev_time - next_time).total_seconds()
            value    += (v * delta)
            seconds  += delta
            max_value = max(max_value, v)
        integrated_value = value / seconds
        if favor_max_value and (max_value > integrated_value):
            return max_value
        return integrated_value

    def set_integrated_float_state(self, key, value, integration_period, TTL=None):
        now    = self.context["now"]
        recs   = self._decode_integrate_float(key, integration_period)
        recs_s = [ r[0] for r in recs]
        recs_s.insert(0, "%s=%s" % (now, float(value)))
        self.set_state(key, ";".join(recs_s), TTL=TTL)


    def _decode_integrate_float(self, key, integration_period):
        now = self.context["now"]
        v = self.get_state(key, None)
        if v is None: 
            records = []
        else:
            records = v.split(";")

        recs = []
        for r in records:
            sp = r.split("=")
            try: 
                d = misc.str2utc(sp[0])
                v = float(sp[1])
                if now - d < timedelta(seconds=integration_period):
                    recs.append(["%s=%s" % (d,v), d, v])
            except:
                pass
        return recs

    def get_instance_control_excluded_instance_ids(self):
        instance_control_states = self.get_instance_control_state()
        ids = []
        for l in ["unstoppable", "unstartable"]:
            lt = instance_control_states[l]
            el = [i for i in lt.keys() if lt[i]["Excluded"]]
            [ids.append(i) for i in el if i not in ids]
        return ids

    def get_instance_control_state(self):
        """ Retrieve the structure describing unstoppable/unstartable instances maintained through the API GW.
        """
        state = self.get_state_json("ec2.control.state", default={
            "unstoppable": {},
            "unstartable": {}
        })
        # Purge obsolete records
        now = misc.seconds_from_epoch_utc()
        for c in state:
            ctrl = state[c]
            for i in list(ctrl.keys()):
                if now > ctrl[i]["TTL"]:
                    del state[c][i]
        return state

    def set_instance_control_state(self, state):
        # Test if we are going to write an empty state so we may optimize it
        min_ttl = self.ttl
        now     = misc.seconds_from_epoch_utc() 
        ttl     = now + min_ttl 
        for c in state:
            ctrl = state[c]
            for i in ctrl.keys():
                ttl = max(ttl, ctrl[i]["TTL"] + min_ttl)
        former_state = self.get_instance_control_state()
        if former_state == state:
            # Former value was already empty => no need to create a record
            return
        self.set_state_json("ec2.control.state", state, TTL=(ttl-now))

    def update_instance_control_state(self, listname, mode, filter_query, ttl_string):
        ctrl = self.get_instance_control_state()
        ttl  = Cfg.get_duration_secs("ec2.instance.control.ttl")
        try:
            if ttl_string: ttl = misc.str2duration_seconds(ttl_string) 
        except Exception as e:
            log.warning(f"Failed to parse TTL value '%s'! Defaulting to {ttl} seconds..." % ttl_string)
        ttl  = misc.seconds_from_epoch_utc(self.context["now"] + timedelta(seconds=ttl))

        # Lookup matching instances
        instances = self.get_instances()
        ids       = [ i["InstanceId"] for i in instances]

        instance_ids = filter_query["InstanceIds"] if "InstanceIds" in filter_query else []
        if "all" in instance_ids:
            instance_ids = ids # Wildcard matches all instances
        else:
            instance_ids = [i for i in instance_ids if i in ids] # Filter out unknown instance id

        for i in instances:
            # Match instance by name
            for tag, values in [("Name", "InstanceNames"), ("clonesquad:subfleet-name", "SubfleetNames")]:
                if not len(filter_query.get(values, [])):
                    continue
                t = next(filter(lambda t: t["Key"] == tag, i["Tags"]), None)
                if (t and t["Value"] in filter_query.get(values, [])) or (not t and not filter_query.get(values, None)):
                    if i["InstanceId"] not in instance_ids:
                        instance_ids.append(i["InstanceId"]) 

        # Match tags specified in query filter
        tags      = filter_query["Tags"] if "Tags" in filter_query else {}
        if len(tags.keys()):
            log.info(f"Matching tags {tags}...")
            for i in instances:
                instance_id = i["InstanceId"]
                if instance_id in instance_ids:
                    continue
                i_tags = {}
                for t in i["Tags"]:
                    i_tags[t["Key"]] = t["Value"]
                #log.info(f"Checking instance {instance_id} with tags {i_tags}...")
                match = False
                for t in tags:
                    # All instance tags must match the query
                    if tags.get(t) is None:
                        if t not in i_tags:
                            match = True
                            continue # Tag not present on the instance as requested
                        match = False
                        break
                    if (t in i_tags and (
                            tags[t] == "*" 
                            or (re.match(tags[t], i_tags[t])) if "*" in tags[t] else False)
                            or (tags[t] == i_tags[t])
                            ):
                        match = True
                        continue # Tag not present or this a different value than the filtered one
                    match = False
                    break
                if match: instance_ids.append(instance_id)
       
        # Perform action according to specified mode
        log.info(f"Action {mode} for list {listname} on instance ids: {instance_ids}.")
        for instance_id in instance_ids:
            if mode == "delete":
                if instance_id in list(ctrl[listname].keys()):
                    del ctrl[listname][instance_id]
            else:
                ctrl[listname][instance_id] = {
                    "TTL": ttl,
                    "StartDate": str(self.context["now"]),
                    "EndDate": str(misc.seconds2utc(ttl)),
                    "Excluded": filter_query.get("Excluded", False)
                }

        # Garbage collection of older instance ids
        for instance_id in ctrl[listname].keys():
            if instance_id not in ids:
                del ctrl[listname][instance_id]
        self.set_instance_control_state(ctrl)
        

    def get_synthetic_metrics(self):
        s_metrics      = []
        az_with_issues = self.get_azs_with_issues()
        for i in self.instances:
            instance_id    = i["InstanceId"]
            is_spot        = self.is_spot_instance(i)
            instance_tags  = self.get_instance_tags(i)
            instance_name  = instance_tags["Name"] if "Name" in instance_tags else None
            subfleet_name  = self.get_subfleet_name_for_instance(i)
            az             = i["Placement"]["AvailabilityZone"]
            located_in_az_with_issues = az in az_with_issues
            instance_state = self.get_scaling_state(instance_id, do_not_return_excluded=True)
            statuses       = [state for state in EC2.INSTANCE_STATES if self.is_instance_state(instance_id, [state])]
            status         = statuses[0] if len(statuses) else "unknown"
            stat = {
                "LocatedInAZWithIssues" : located_in_az_with_issues,
                "InstanceName": instance_name,
                "InstanceType": i["InstanceType"],
                "Tags"        : i["Tags"],
                "InstanceId"  : instance_id,
                "SpotInstance": is_spot,
                "SubfleetName": subfleet_name,
                "AvailabilityZone": az,
                "Status"      : status,
                "State"       : i["State"]["Name"] if instance_state not in ["draining", "error", "bounced"] else instance_state
            }
            targetgroups            = self.context["o_targetgroup"].get_targetgroups()
            stat["TargetGroups"]    = {
                "NbOfTargetGroups": len(targetgroups),
                "Arns": [t["TargetGroupArn"] for t in targetgroups]
            }
            targetgroups            = self.context["o_targetgroup"].get_targetgroups()
            stat["TargetGroups"]    = {
                "NbOfTargetGroups": len(targetgroups),
                "Arns": [t["TargetGroupArn"] for t in targetgroups]
            }
            if is_spot:
                stat["SpotDetails"] = {
                        "InterruptedAt" : EC2.get_spot_event(self.context, instance_id, "interrupted"),
                        "RebalanceRecommendedAt" : EC2.get_spot_event(self.context, instance_id, "rebalance_recommended")
                }
            s_metrics.append(stat)
        return s_metrics


###############################################
#### SPOT INSTANCE MANAGEMENT #################
###############################################

    def is_spot_instance(self, i):
        return "SpotInstanceRequestId" in i

    @staticmethod
    def get_spot_event(ctx, instance_id, reason, default=None):
        v = ctx["o_ec2"].get_state("ec2.instance.spot.event.%s.%s_at" % (instance_id, reason))
        try:
            if v is None or (ctx["now"] - misc.str2utc(v)).total_seconds() > Cfg.get_duration_secs(f"ec2.instance.spot.event.{reason}_at_ttl"):
                return default
        except:
            return default
        return v

    @staticmethod
    def set_spot_event(ctx, instance_id, reason, now):
        ctx["o_ec2"].set_state("ec2.instance.spot.event.%s.%s_at" % (instance_id, reason), now,
            TTL=Cfg.get_duration_secs("ec2.instance.spot.event.%s_at_ttl" % reason))
        ctx["o_ec2"].set_state("cache.last_write_index", ctx["now"]) # Force state cache flush

def manage_spot_notification(sqs_record, ctx):
    try:
        body = json.loads(sqs_record["body"])
    except:
        return False
    if not "detail-type" in body:
        return False

    if body["detail-type"] == "EC2 Spot Instance Interruption Warning":
        reason = "interrupted"
        func   = spot_interruption_request
    elif body["detail-type"] == "EC2 Instance Rebalance Recommendation":
        reason = "rebalance_recommended"
        func   = spot_rebalance_recommandation_request
    else:
        return False
    log.log(log.NOTICE, json.dumps(sqs_record))

    now         = ctx["now"]
    instance_id = body["detail"]["instance-id"]
    ctx["o_state"].get_prerequisites()
    ctx["o_notify"].get_prerequisites()
    ctx["o_ec2"].get_prerequisites(only_if_not_already_done=True)

    log.info("EC2 Spot instance '%s' received event '%s'! " % (instance_id, reason))
    EC2.set_spot_event(ctx, instance_id, reason, now)

    # Notify interested entities about the event
    R(None, func, InstanceId=instance_id, Event=body)

    return True

def spot_interruption_request(InstanceId=None, Event=None):
    return {}

def spot_rebalance_recommandation_request(InstanceId=None, Event=None):
    return {}


