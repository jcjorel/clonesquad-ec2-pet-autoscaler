import boto3
import json
import pdb
import re
import sys
import yaml
from datetime import datetime
from datetime import timedelta
from collections import defaultdict

import misc
import kvtable
import config as Cfg
import debug as Dbg
from notify import record_call as R

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

        Cfg.register({
                 "ec2.describe_instances.max_results" : "250",
                 "ec2.describe_instance_types.enabled": "0",
                 "ec2.az.statusmgt.disable": 0,
                 "ec2.az.unavailable_list,Stable": {
                     "DefaultValue": "",
                     "Format"      : "StringList",
                     "Description" : """List of Availability Zone names (ex: *eu-west-3c*) or AZ Ids (ex: *euw3-az1*).

Typical usage is to force a fleet to consider one or more AZs as unavailable (AZ eviction). The autoscaler will then refuse to schedule
new instances on these AZs. Existing instances in those AZs are left unchanged but on scalein condition will be 
shutdown in priority (see [`ec2.az.evict_instances_when_az_faulty`](#ec2azinstance_faulty_when_az_faulty) to change this behavior). 

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
                 "ec2.debug.availability_zones_impaired": "",
                 "ec2.instance.status.override_url,Stable": {
                    "DefaultValue": "",
                    "Format"      : "String",
                    "Description" : """Url pointing to a YAML file overriding EC2.describe_instance_status() instance state.

                    CloneSquad can optionally load a YAML file containing EC2 instance status override.

The format is a dict of 'InstanceId' containing another dict of metadata:

```yaml
---
i-0ef23917a58368c89:
    status: ok
i-0ad73bbc09cb68f81:
    status: unhealthy
```

The status item can contain any of valid values from EC2.describe_instance_status()["InstanceStatus"]["Status"].
These valid values are ["ok", "impaired", "insufficient-data", "not-applicable", "initializing", "unhealthy"].    

**Please notice the special 'unhealthy' value that is a cloneSquad extension:** This value can be injected to force 
an instance to be considered as unhealthy by the scheduler. It can be useful to debug/simulate a failure of a 
specific instance or to inject 'unhealthy' status coming from a non-TargetGroup source (ex: when CloneSquad is used
without any TargetGroup but another external health instance source exists).

                    """
                 }
        })

        self.o_state.register_aggregates([
            {
                "Prefix": "ec2.instance.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("ec2.state.default_ttl"),
                "Exclude" : ["ec2.instance.scaling.state."]
            }
            ])


    def get_prerequisites(self, only_if_not_already_done=False):
        if only_if_not_already_done and self.prereqs_done:
            return

        self.state_table = self.o_state.get_state_table()
        client           = self.context["ec2.client"]

        # Retrieve list of instances with appropriate tag
        Filters          = [{'Name': 'tag:clonesquad:group-name', 'Values': [self.context["GroupName"]]}]
        
        instances = []
        response = None
        while (response is None or "NextToken" in response):
            response = client.describe_instances(Filters=Filters,
                    MaxResults=Cfg.get_int("ec2.describe_instances.max_results"),
                    NextToken=response["NextToken"] if response is not None else "")
            for reservation in response["Reservations"]:
                instances.extend(reservation["Instances"])

        # Filter out instances with inappropriate state
        non_terminated_instances = []
        for i in instances:
            if i["State"]["Name"] not in ["shutting-down", "terminated"]:
                non_terminated_instances.append(i)

        self.instances    = non_terminated_instances
        self.instance_ids = [ i["InstanceId"] for i in self.instances]

        # Enrich describe_instances output with instance type details
        if Cfg.get_int("ec2.describe_instance_types.enabled"):
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
        while response is None or "NextToken" in response:
            q = { "InstanceIds": self.instance_ids }
            if response is not None and "NextToken" in response: q["NextToken"] = response["NextToken"]
            response = client.describe_instance_status(**q)
            instance_statuses.extend(response["InstanceStatuses"])
        self.instance_statuses = instance_statuses

        # Get AZ status
        response = client.describe_availability_zones()
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

        # We need to register dynamically static subfleet configuration keys to avoid a 'key unknown' warning 
        #   when the user is going to set it
        static_subfleet_names = self.get_static_subfleet_names()
        for static_fleet in static_subfleet_names:
            key = "staticfleet.%s.state" % static_fleet
            if not Cfg.is_builtin_key_exist(key):
                Cfg.register({
                    key : ""
                    })
        log.log(log.NOTICE, "Detected following static subfleet names across EC2 resources: %s" % static_subfleet_names)

        # Load EC2 status override URL content
        self.ec2_status_override_url = Cfg.get("ec2.instance.status.override_url")
        if self.ec2_status_override_url is not None and self.ec2_status_override_url != "":
            try:
                content = misc.get_url(self.ec2_status_override_url)
                self.ec2_status_override = yaml.safe_load(str(content, "utf-8"))
            except Exception as e:
                log.warning("Failed to load 'ec2.instance.status.override_url' YAML file '%s' : %s" % (self.ec2_status_override_url, e))

        self.prereqs_done    = True

    def register_state_aggregates(self, aggregates):
        self.o_state.register_aggregates(aggregates)

    def get_instance_statuses(self):
        return self.instance_statuses

    def is_instance_state(self, instance_id, state):
        i = next(filter(lambda i: i["InstanceId"] == instance_id, self.instance_statuses), None)

        # Check for "az_evicted" synthetic status
        if Cfg.get_int("ec2.az.evict_instances_when_az_faulty") and "az_evicted" in state:
            az = self.get_instance_by_id(instance_id)["Placement"]["AvailabilityZone"]
            if az in self.get_azs_with_issues():
                return True

        # Check if the status of this instance Id is overriden with an external YAML file
        if i is not None and i["InstanceState"]["Name"] in ["pending", "running"] and instance_id in self.ec2_status_override:
            override = self.ec2_status_override[instance_id]
            if "status" in override:
                override_status = override["status"]
                if override_status not in ["ok", "impaired", "insufficient-data", "not-applicable", "initializing", "unhealthy"]:
                    log.warning("Status override for instance '%s' (defined in %s) has an unmanaged status (%s) !" % 
                            (instance_id, self.ec2_status_override_url, override_status))
                else:
                    return override_status in state

        return i["InstanceStatus"]["Status"] in state if i is not None else False

    def get_azs_with_issues(self):
        return [ az["ZoneName"] for az in self.az_with_issues ]

    def get_static_subfleet_instances(self, subfleet_name=None):
        instances = self.filter_instance_list_by_tag(self.instances, "clonesquad:static-subfleet-name", subfleet_name)
        return self.filter_instance_list_by_tag(instances, "-clonesquad:excluded", ["True", "true"])

    def get_static_subfleet_names(self):
        instances = self.get_static_subfleet_instances()
        names     = []
        for i in instances:
            tags = self.get_instance_tags(i)
            [names.append(tags[k]) for k in tags if k == "clonesquad:static-subfleet-name" and tags[k] not in names]
        return names

    def get_static_subfleet_name_for_instance(self, i):
        return self.get_instance_tags(i)["clonesquad:static-subfleet-name"]

    def is_static_subfleet_instance(self, instance_id, subfleet_name=None):
        instances    = self.get_static_subfleet_instances(subfleet_name=subfleet_name)
        instance_ids = [i["InstanceId"] for i in instances]
        return instance_id in instance_ids

    def get_timesorted_instances(self, instances=None):
        if instances is None: instances=self.instances
        # Sort instance list starting from the oldest launch to the newest
        def _compare_start_date(i):
            instance_id = i["InstanceId"]
            last_start_attempt = self.get_state_date("ec2.instance.last_start_attempt_date.%s" % instance_id)
            if last_start_attempt is not None and last_start_attempt > i["LaunchTime"]:
                return last_start_attempt
            return i["LaunchTime"]
        return sorted(instances, key=_compare_start_date)

    def get_instances(self, instances=None, State=None, ScalingState=None, details=None, max_results=-1, azs_filtered_out=None):
        if details is None: details = {}
        details.update({
            "state" : {"filtered-in": [],
                "filtered-out": []},
            "scalingstate" : {"filtered-in": [],
                "filtered-out": []},
            })

        ref_instances = self.instances if instances is None else instances
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
        # Remember when we tried to start all these instances. Used to detect instances with issues
        #    by placing them at end of get_instances() generated list
        if instance_ids_to_start is None or len(instance_ids_to_start) == 0:
            return 
        now = self.context["now"]

        client = self.context["ec2.client"]
        for i in instance_ids_to_start: 
            if max_started_instances == 0:
                break
            self.set_state("ec2.instance.last_start_attempt_date.%s" % i, now,
                    TTL=Cfg.get_duration_secs("ec2.schedule.state_ttl"))

            log.info("Starting instance %s..." % i)
            response = None
            try:
                response = R(lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                    client.start_instances, InstanceIds=[i]
                )
            except Exception as e:
                log.exception("Got Exception while trying to start instance '%s' : %s" % (i, e))
                # Mark the instance in error only if the status is not 'running'
                #   With Spot instances, from time-to-time, we catch an 'InsufficientCapacityError' even the
                #   instance succeeded to start. We issue a describe_instances to check the real state of this
                #   instance to confirm/infirm the status
                if response is not None:
                    response = R(lambda args, kwargs, r: "Reservations" in r and len(response["Reservations"]["Instances"]),
                        client.describe_instances, InstanceIds=[i]
                    )
                if (response is None or "Reservations" not in response
                        or len(response["Reservations"][0]["Instances"]) == 0 
                        or response["Reservations"][0]["Instances"][0]["State"]["Name"] not in ["pending", "running"]):
                    self.set_scaling_state(i, "error", ttl=Cfg.get_duration_secs("ec2.state.error_ttl"))
                    continue
            if response is not None: log.debug(Dbg.pprint(response))

            # Remember when we started these instances
            metadata = response["ResponseMetadata"]
            if metadata["HTTPStatusCode"] == 200:
                s = response["StartingInstances"]
                for r in s:
                    instance_id    = r["InstanceId"]
                    previous_state = r["PreviousState"]
                    current_state  = r["CurrentState"]
                    if current_state["Name"] in ["pending", "running"]:
                        self.set_state("ec2.instance.last_start_date.%s" % instance_id, now,
                                TTL=Cfg.get_duration_secs("ec2.state.status_ttl"))
                        max_started_instances -= 1
                    else:
                        log.error("Failed to start instance '%s'! Blacklist it for a while... (pre/current status=%s/%s)" %
                                (instance_id, previous_state["Name"], current_state["Name"]))
                        self.set_scaling_state(instance_id, "error", ttl=Cfg.get_duration_secs("ec2.state.error_ttl"))
                        R(None, self.instance_in_error, Operation="start", InstanceId=instance_id, 
                                PreviousState=previous_state["Name"], CurrentState=current_state["Name"])
            else:
                log.error("Failed to call start_instances: %s" % i)

    def stop_instances(self, instance_ids_to_stop):
        now    = self.context["now"]
        client = self.context["ec2.client"]
        for instance_id in instance_ids_to_stop:
            try:
                response = R(lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                        client.stop_instances, InstanceIds=[instance_id]
                   )
                if response is not None and "StoppingInstances" in response:
                    for i in response["StoppingInstances"]:
                        instance_id = i["InstanceId"]
                        self.set_scaling_state(instance_id, "")
                        self.set_state("ec2.schedule.instance.last_stop_date.%s" % instance_id, now, 
                                TTL=Cfg.get_duration_secs("ec2.state.status_ttl"))
                log.debug(response)
            except Exception as e:
                log.warning("Failed to stop_instance '%s' : %s" % (instance_id, e))

    def instance_last_stop_date(self, instance_id, default=misc.epoch()):
        return self.get_state_date("ec2.schedule.instance.last_stop_date.%s" % instance_id, default=default)

    def instance_in_error(self, Operation=None, InstanceId=None, PreviousState=None, CurrentState=None):
        return {}

    def sort_by_prefered_azs(self, instances, prefered_azs=None, prefered_before=True):
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
        if instance is None:
            return default
        tags = {}
        for t in instance["Tags"]:
            tags[t["Key"]] = t["Value"]
        return tags


    def instance_has_tag(self, instance, tag, value=None):
        if instance is None:
            return None
        for t in instance["Tags"]:
            if t["Key"] != tag:
                continue
            if value is None:
                return t["Value"]
            return t["Value"] if t["Value"] in value else None 
        return None

    def filter_instance_recently_stopped(self, instances, min_age, filter_only_spot=True):
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
        r = defaultdict(list)
        for i in self.get_instances():
            instance_id = i["InstanceId"]
            state = self.get_scaling_state(instance_id, default="unknown")
            r[state].append(instance_id)
        return dict(r)

    def get_scaling_state(self, instance_id, default=None, meta=None, default_date=None, do_not_return_excluded=False):
        if meta is not None:
            for i in ["action", "draining", "error", "bounced"]:
                meta["last_%s_date" % i] = misc.str2utc(self.get_state("ec2.instance.scaling.last_%s_date.%s" %
                        (i, instance_id), default=self.context["now"]))
        r = self.get_state("ec2.instance.scaling.state.%s" % instance_id, default=default)
        #Special case for 'excluded': We test it here so tags will override the value
        i = self.get_instance_by_id(instance_id)
        excluded_instances = Cfg.get_list("ec2.state.excluded_instance_ids", default=[])
        if (i is not None and not do_not_return_excluded and (
                self.instance_has_tag(i, "clonesquad:excluded", value=["1", "True", "true"])
                or i in excluded_instances
                or self.is_static_subfleet_instance(instance_id))):
            r = "excluded"
        # Force error state for some VM (debug usage)
        error_instance_ids = Cfg.get_list("ec2.state.error_instance_ids", default=[]) 
        if instance_id in error_instance_ids:
            r = "error"
        return r

    def set_scaling_state(self, instance_id, value, ttl=None, meta=None, default_date=None):
        if ttl is None: ttl = Cfg.get_duration_secs("ec2.state.default_ttl") 
        if default_date is None: default_date = self.context["now"]
        #if value in ["draining"] and instance_id in ["i-0ed9bddf74dd2a2f5", "i-0904bbd267f736227"]: pdb.set_trace()

        meta           = {} if meta is None else meta
        previous_value = self.get_scaling_state(instance_id, meta=meta, do_not_return_excluded=True)
        date           = meta["last_action_date"] if previous_value == value else default_date
        self.set_state("ec2.instance.scaling.last_action_date.%s" % instance_id, date, ttl)
        self.set_state("ec2.instance.scaling.last_%s_date.%s" % (value, instance_id), date, ttl)
        previous_value = self.get_scaling_state(instance_id, meta=meta)
        return self.set_state("ec2.instance.scaling.state.%s" % instance_id, value, ttl)

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

    def get_state(self, key, default=None, direct=False):
        if self.state_table is None or direct: 
            return kvtable.KVTable.get_kv_direct(key, self.context["StateTable"], default=default)
        return self.state_table.get_kv(key, default=default, direct=direct)

    def get_state_int(self, key, default=0, direct=False):
        try:
            return int(self.get_state(key, direct=direct))
        except:
            return default

    def get_state_json(self, key, default=None, direct=False):
        try:
            return misc.decode_json(self.get_state(key, default=default, direct=direct))
        except:
            return default

    def set_state_json(self, key, value, compress=True, TTL=0):
        self.set_state(key, misc.encode_json(value, compress=compress), TTL=TTL)

    def get_state_date(self, key, default=None, direct=False):
        d = self.get_state(key, default=default, direct=direct)
        if d is None or d == "": return default
        try:
            date = datetime.fromisoformat(d)
        except:
            return default
        return date

    def set_state(self, key, value, TTL=None):
        self.state_table.set_kv(key, value, TTL=TTL)

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
