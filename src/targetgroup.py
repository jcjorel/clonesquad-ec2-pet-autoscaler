import boto3
import json
import pdb
import re
import time
import random

import config as Cfg
import debug as Dbg
from notify import record_call as R

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class ManagedTargetGroup:
    @xray_recorder.capture(name="ManagedTargetGroup.__init__")
    def __init__(self, context, ec2):
        self.context       = context
        self.ec2           = ec2
        self.client_ec2    = context["ec2.client"]
        self.client_elbv2  = context["elbv2.client"]
        self.state_changed = False
        self.prereqs_done  = False
        Cfg.register({
            "targetgroup.debug.inject_fault_status": "",
            "targetgroup.default_state_ttl": "minutes=30",
            "targetgroup.slow_deregister_timeout": "minutes=2"
        })
        self.ec2.register_state_aggregates([
            {
                "Prefix": "targetgroup.status.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("targetgroup.default_state_ttl")
            }
            ])


    def get_prerequisites(self, only_if_not_already_done=False):
        if only_if_not_already_done and self.prereqs_done:
            return

        response = self.client_elbv2.describe_target_groups()
        targetgroups = response["TargetGroups"]
        targetgroups_arns = [t["TargetGroupArn"] for t in targetgroups]
        response = self.client_elbv2.describe_tags(
            ResourceArns=targetgroups_arns
            )

        for tags in response["TagDescriptions"]:
            arn = tags["ResourceArn"]
            targetgroup = next(filter(lambda x: x["TargetGroupArn"] == arn, targetgroups))
            targetgroup["Tags"] = tags["Tags"]
        
        self.targetgroups = []
        for t in targetgroups:
            tag = next(filter(lambda x: x["Key"] == "clonesquad:group-name" and x["Value"] == self.context["GroupName"], t["Tags"]), None)
            if tag is not None:
                self.targetgroups.append(t)       

        for i in self.targetgroups:
            response = self.client_elbv2.describe_target_health(
               TargetGroupArn=i["TargetGroupArn"])
            if "TargetHealthDescriptions" not in response:
               raise Exception("Failed to retrieve TargetHealthDescriptions for '%s'!" % i["TargetGroupArn"])
            i["TargetHealthDescriptions"] = response["TargetHealthDescriptions"]

        directives = Cfg.get("targetgroup.debug.inject_fault_status")
        if directives is not None and directives != "":
            log.warning("'targetgroup.debug.inject_fault_status' set to %s!" % directives)

        self.prereqs_done = True


    def debug_inject_fault(self, instance_id, targetgroup, default, nolog=False):
        instance = self.ec2.get_instance_by_id(instance_id)
        if instance is None or instance["State"]["Name"] != "running": return default

        # If AWS indicate issues with some AZ, we assume instances located in them are 'unavail'
        if instance["Placement"]["AvailabilityZone"] in self.ec2.get_azs_with_issues(): return "unavail"

        directives = Cfg.get("targetgroup.debug.inject_fault_status").split(",")

        for directive in directives:
            if directive == "": continue

            criteria, fault = directive.split(":")
            c = criteria.split("&")
            criteria = c[0]
            # Check if a targetgroup name constraint is set
            if len(c) > 1:
                if targetgroup is not None and self.get_short_targetgroup_name(targetgroup) not in c:
                    continue
            instance_id = instance["InstanceId"]
            if criteria == instance_id or criteria == instance["Placement"]["AvailabilityZone"]:
                if not nolog:
                    log.warning("Injecting targetgroup fault '%s/%s' for instance '%s'!" % 
                            (targetgroup if targetgroup is not None else "All targetgroups", fault, instance_id))
                return fault
        return default

    def is_state_changed(self):
        return self.state_changed


    def set_instance_state(self, instance_id, targetgroup_name, value):
        m                = re.search("(.*)/([^/]+)/\w+$", targetgroup_name)
        targetgroup_name = m.group(2) 
        key = "targetgroup.status.%s.%s" % (targetgroup_name, instance_id)
        self.ec2.set_state(key.replace(":", "_"), value, TTL=Cfg.get_duration_secs("targetgroup.default_state_ttl"))

    def get_instance_state(self, instance_id, targetgroup_name):
        m                = re.search("(.*)/([^/]+)/\w+$", targetgroup_name)
        targetgroup_name = m.group(2) 
        key = "targetgroup.status.%s.%s" % (targetgroup_name, instance_id)
        return self.ec2.get_state(key.replace(":", "_"))

    def get_short_targetgroup_name(self, targetgrouparn):
        # arn:aws:elasticloadbalancing:eu-west-1:<Id>:targetgroup/<groupname>/5497e211589cb4cb
        m = re.search(".*:targetgroup/(.*)/[a-z0-9]+", targetgrouparn)
        if len(m.groups()) > 0:
            return m.group(1)
        return None

    def get_registered_targets(self, targetgroup=None, state=None, nolog=False):
        targets = []
        for t in self.targetgroups:
            if targetgroup is not None and t["TargetGroupArn"] != targetgroup:
                continue
            ts = []
            for target in t["TargetHealthDescriptions"]:
                if "Id" not in target["Target"]:
                    continue
                instance_id = target["Target"]["Id"]
                if (state is None or 
                        self.debug_inject_fault(instance_id, targetgroup, target["TargetHealth"]["State"], nolog=nolog) in state.split(",")):
                    ts.append(target)
            targets.append(ts)
        return targets

    def get_targetgroups_info(self):
        active_instances = self.ec2.get_running_instances()
        info = {
                "TargetGroupInfo" : [],
                "AllUnuseableTargetIds" : [],
            }
        unuseable_target = info["AllUnuseableTargetIds"]
        for t in self.targetgroups:
            targetgrouparn = t["TargetGroupArn"]
            unuseable_target_instance_ids = self.get_registered_instance_ids(targetgroup=targetgrouparn, state="draining,unavail,unhealthy")
            useable_instances_count = len(active_instances) - len(unuseable_target_instance_ids)  
            for i in unuseable_target_instance_ids:
                if i not in unuseable_target_instance_ids: unuseable_target_instance_ids.append(i)
                if i not in info["AllUnuseableTargetIds"]: info["AllUnuseableTargetIds"].append(i)

            info["TargetGroupInfo"].append({
                    "TargetGroupArn": targetgrouparn,
                    "UseableInstanceCount"  : useable_instances_count, 
                    "UnuseableInstanceCount": len(unuseable_target_instance_ids), 
                    "UnuseableInstanceIds" : unuseable_target_instance_ids
                })

        info["MaxUnuseableTargetsOverTargetGroups"] = len(info["AllUnuseableTargetIds"])
        log.debug(Dbg.pprint(info))
        return info

    def get_registered_instance_ids(self, targetgroup=None, state=None):
        ids = []
        for t in self.get_registered_targets(targetgroup=targetgroup, state=state, nolog=True):
            for target in t:
                if "Id" not in target["Target"]:
                    continue
                instance_id = target["Target"]["Id"]
                if self.debug_inject_fault(instance_id, targetgroup, target["TargetHealth"]["State"], nolog=True) in state.split(","):
                    if instance_id not in ids: ids.append(instance_id)
        return ids

    def is_instance_registered(self, targetgroup, instance_id, fail_if_draining=False):
        t = next(filter(lambda target: targetgroup is None or target["TargetGroupArn"] == targetgroup, self.targetgroups), None)
        if t is None:
            return None
        h = next(filter(lambda target: target['Target']['Id'] == instance_id, t["TargetHealthDescriptions"]), None)
        if fail_if_draining and h['TargetHealth']["State"] == 'draining':
            return None
        return h


    @xray_recorder.capture()
    def manage_targetgroup(self):
        """
        Add and remove EC2 instances in optionally user supplied TargetGroup
        """
        running_instances  = self.ec2.get_instances(State="running", ScalingState="-excluded")

        # Add excluded instances explicitly marked for forced inclusion to targetgroups
        excluded_instances = self.ec2.get_instances(State="running", ScalingState="excluded")
        running_instances.extend(self.ec2.filter_instance_list_by_tag(excluded_instances, 
            "clonesquad:force-excluded-instance-in-targetgroups", value=["True", "true"]))

        transitions = []
        for target in self.targetgroups:
            self._manage_targetgroup(target["TargetGroupArn"], running_instances, transitions)

        if len(transitions):
            R(None, self.targetgroup_transitions, Transitions=transitions)

    def targetgroup_transitions(self, Transitions=None):
        return {}

    def _manage_targetgroup(self, targetgroup, running_instances, transitions):
        now                = self.context["now"]
        registered_targets = self.get_registered_targets(targetgroup)[0]

        #  Generate events on instance state transition 
        for instance in self.ec2.get_instances(ScalingState="-excluded"):
            instance_id    = instance["InstanceId"]
            previous_state = self.get_instance_state(instance_id, targetgroup)
            if previous_state is None: previous_state = "None"
            target_instance = self.is_instance_registered(targetgroup, instance_id)
            current_state = target_instance['TargetHealth']["State"] if target_instance is not None else "None"
            if current_state != previous_state:
                transitions.append({
                        "InstanceId": instance_id,
                        "TargetGroupArn": targetgroup,
                        "PreviousState" : previous_state,
                        "NewState": current_state
                    })
            self.set_instance_state(instance_id, targetgroup, current_state)
        
        # List instances that are running and not yet in the TargetGroup
        instance_ids_to_add = []
        for instance in running_instances:
            instance_id = instance["InstanceId"]
            if self.ec2.get_scaling_state(instance_id) in ["draining", "bounced", "error"]:
                continue

            target_instance = self.is_instance_registered(targetgroup, instance_id)
            if target_instance is None:
                instance_ids_to_add.append({'Id':instance_id})
                self.set_instance_state(instance_id, targetgroup, "None")


        if len(instance_ids_to_add) > 0:
            log.debug("Registering instance(s) in TargetGroup: %s" % instance_ids_to_add)
            for instance_id in instance_ids_to_add:
                try:
                    response = R(lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                        self.client_elbv2.register_targets, TargetGroupArn=targetgroup, Targets=[instance_id] 
                    )
                except Exception as e:
                    log.exception("Failed to register target '%s' in targetgroup '%s'!' : %s" % 
                            (instance_id, targetgroup["TargetGroupArn"], e))
            self.state_changed = True

        if self.state_changed:
            return

        # When there are instances in initial state, we have to react slower to
        #    misbehavior if 'initial' instances fail their health checks.
        slow_deregister = len(self.get_registered_instance_ids(state="initial")) != 0

        # List instances that are no more running but still in the TargetGroup
        delayed_deregister_instance_ids = []
        instance_ids_to_delete          = []
        draining_instances              = self.ec2.get_instances(ScalingState="excluded,draining,bounced,error")
        slow_deregister_timeout         = int(Cfg.get_duration_secs("targetgroup.slow_deregister_timeout"))
        for instance in registered_targets:
            instance_id = instance["Target"]["Id"]
            instance = self.ec2.get_instance_by_id(instance_id)

            if self.is_instance_registered(targetgroup, instance_id, fail_if_draining=True) is None:
                continue

            if instance is None or instance["State"]["Name"] not in ["pending","running"] or instance_id in self.ec2.get_instance_ids(draining_instances):
                meta                    = {}
                self.ec2.get_scaling_state(instance_id, meta=meta)
                if meta["last_action_date"] is not None and slow_deregister:
                    gap_secs                = (now - meta["last_action_date"]).total_seconds()
                    if gap_secs < (slow_deregister_timeout * random.random()):
                        if instance_id not in [ i["InstanceId"] for i in delayed_deregister_instance_ids]: 
                            delayed_deregister_instance_ids.append({
                            "InstanceId": instance_id,
                            "Gap": gap_secs
                            })
                        continue
                instance_ids_to_delete.append({'Id':instance_id})

        for i in delayed_deregister_instance_ids:
            log.info("Slow deregister mode: Instance '%s' is waiting deregister for %d seconds... (targetgroup.slow_deregister_timeout=%s + jitter...)" % 
                    (i["InstanceId"], i["Gap"], slow_deregister_timeout))


        if len(instance_ids_to_delete) > 0:
            log.debug("Deregistering instance(s) in TargetGroup: %s" % instance_ids_to_delete)
            response = R(lambda args, kwargs, r: r["ResponseMetadata"]["HTTPStatusCode"] == 200,
                self.client_elbv2.deregister_targets, TargetGroupArn=targetgroup, Targets=instance_ids_to_delete
            )
            self.state_changed = True
