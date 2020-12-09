import math
import re
import boto3
import json
import yaml
import pdb
import hashlib
import random
from datetime import datetime
from datetime import timedelta
from collections import defaultdict

import notify
from notify import record_call as R
import sqs
import kvtable
import misc
import config as Cfg
import debug as Dbg

from aws_xray_sdk.core import xray_recorder

import cslog
log = cslog.logger(__name__)

class EC2_Schedule:
    @xray_recorder.capture(name="EC2_Schedule.__init__")
    def __init__(self, context, ec2, targetgroup, cloudwatch):
        self.context                  = context
        self.ec2                      = ec2
        self.targetgroup              = targetgroup
        self.cloudwatch               = cloudwatch
        self.scaling_state_changed    = False
        self.alarm_points             = 0.0
        self.instance_scale_score     = 0.0
        self.raw_instance_scale_score = 0.0
        self.integrated_raw_instance_scale_score = 0.0
        self.would_like_to_scalein    = False
        self.would_like_to_scaleout   = False
        self.spot_rebalance_recommanded   = []
        self.spot_interrupted         = []
        self.excluded_spot_instance_types = []
        self.spot_excluded_instance_ids = []
        self.letter_box_subfleet_to_stop_drained_instances = defaultdict(int)
        Cfg.register({
                 "ec2.schedule.min_instance_count,Stable" : {
                     "DefaultValue" : 0,
                     "Format"       : "PositiveInteger",
                     "Description"  : """Minimum number of healthy serving instances. 

CloneSquad will ensure that at least this number of instances
are runnning at a given time. A serving instance has passed all the health checks (if part of Target group) and is not detected with
system issues."""
                  },
                 "ec2.schedule.desired_instance_count,Stable" : {
                     "DefaultValue" : -1,
                     "Format"       : "IntegerOrPercentage", 
                     "Description"  : """If set to -1, the autoscaler controls freely the number of running instances. Set to a value different than -1,
the autoscaler is disabled and this value defines the number of serving (=running & healthy) instances to maintain at all time.
The [`ec2.schedule.min_instance_count`](#ec2schedulemin_instance_count) is still authoritative and the `ec2.schedule.desired_instance_count` parameter cannot bring
the serving fleet size below this hard lower limit. 

A typical usage for this key is to set it to `100%` to temporarily force all the instances to run at the same time to perform mutable maintenance
(System and/or SW patching).

> Tip: Setting this key to the special `100%` value has also the side effect to disable all instance health check management and so ensure the whole fleet running 
at its maximum size in a stable manner (i.e. even if there are impaired/unhealthy instances in the fleet, they won't be restarted automatically).
                     """
                 },
                 "ec2.schedule.max_instance_start_at_a_time" : 10,
                 "ec2.schedule.max_instance_stop_at_a_time" : 5,
                 "ec2.schedule.state_ttl" : "hours=2",
                 "ec2.schedule.base_points" : 1000,
                 "ec2.schedule.scaleout.disable,Stable": {
                     "DefaultValue" : 0,
                    "Format"        : "Bool",
                    "Description"   : """Disable the scaleout part of the autoscaler.

    Setting this value to 1 makes the autoscaler scalein only.
                    """
                 },
                 "ec2.schedule.scaleout.rate,Stable": {
                     "DefaultValue" : 5,
                     "Format"       : "PositiveInteger",
                     "Description"  : """Number of instances to start per period.   

This core parameter is used by the autoscaler to compute when to 
start a new instance under a scaleout condition. By default, it is set to 5 instances per period (see [`ec2.schedule.scaleout.period`](#ec2schedulescaleoutperiod)) that 
is quite a slow growth rate. This number can be increased to grow faster the running fleet under scale out condition.

Increasing too much this parameter makes the autoscaler very reactive and can lead to over-reaction inducing unexpected costs:
A big value can be sustainable when [CloudWatch High-Precision alarms](https://aws.amazon.com/fr/about-aws/whats-new/2017/07/amazon-cloudwatch-introduces-high-resolution-custom-metrics-and-alarms/)
are used allowing the autoscaler to get very quickly a 
feedback loop of the impact of added instances. With standard alarms from the AWS namespace, precision is at best 1 minute and 
the associated CloudWatch metrics can't react fast enough to inform the algorithm with accurate data.

> **Do not use big value there when using Cloudwatch Alarms with 1 min ou 5 mins precision.**
                     """
                 },
                 "ec2.schedule.scaleout.period,Stable": {
                     "DefaultValue" : "minutes=10",
                     "Format"       : "Duration",
                     "Description"  : """Period of scaling assesment. 

This parameter is strongly linked with [`ec2.scheduler.scaleout.rate`](#ec2schedulerscaleoutrate) and is 
used by the scaling algorithm as a devider to determine the fleet growth rate under scalout condition.
                     """
                 },
                 "ec2.schedule.scaleout.instance_upfront_count,Stable" : {
                     "DefaultValue" : 1,
                     "Format"       : "Integer",
                     "Description"  : """Number of instances to start upfront of a new scaleout condition.

When autoscaling is enabled, the autoscaler algorithm compute when to start a new instance using its internal time-based and point-based
algorithm. This parameter is used to bypass this algorithm (only at start of a scaleout sequence) and make it appears more responsive
by starting immediatly the specified amount of instances. 

> It is not recommended to put a big value for this parameter (it is better 
to let the autoscaler algorithm do its smoother job instead)
                     """
                 },
                 "ec2.schedule.scalein.disable,Stable": {
                     "DefaultValue"  : 0,
                     "Format"        : "Bool",
                     "Description"   : """Disable the scalein part of the autoscaler.

    Setting this value to 1 makes the autoscaler scaleout only.
                     """
                 },
                 "ec2.schedule.scalein.rate,Stable": {
                     "DefaultValue": 3,
                     "Format"      : "Integer",
                     "Description" : """Same than `ec2.schedule.scaleout.rate` but for the scalein direction.

Must be a greater than `0` Integer.

                     """
                 },
                 "ec2.schedule.scalein.period,Stable": {
                     "DefaultValue": "minutes=10",
                     "Format"      : "Duration",
                     "Description" : """Same than `ec2.schedule.scaleout.period` but for the scalein direction.

Must be a greater than `0` Integer.

                     """
                 },
                 "ec2.schedule.scalein.instance_upfront_count,Stable" : {
                         "DefaultValue": 0,
                         "Format"      : "Integer",
                         "Description" : """Number of instances to stop upfront of a new scalein condition

When autoscaling is enabled, the autoscaler algorithm compute when to drain and stop a new instance using its internal time-based and point-based
algorithm. This parameter is used to bypass this algorithm (only at start of a scalein sequence) and make it appears more responsive
by draining immediatly the specified amount of instances. 

> It is not recommended to put a big value for this parameter (it is better 
to let the autoscaler algorithm do its smoother job instead)
                         """
                 },
                 "ec2.schedule.scalein.threshold_ratio" : 0.66,
                 "ec2.schedule.to_scalein_state.cooldown_delay" : 120,
                 "ec2.schedule.to_scaleout_state.cooldown_delay" : 0,
                 "ec2.schedule.horizontalscale.unknown_divider_target": "0.8",
                 "ec2.schedule.horizontalscale.integration_period": "minutes=5",
                 "ec2.schedule.horizontalscale.raw_integration_period": "minutes=10",
                 "ec2.schedule.verticalscale.instance_type_distribution,Stable": {
                         "DefaultValue": "",
                         "Format"      : "MetaStringList",
                         "Description" : """Policy for vertical scaling. 

This setting is a core critical one and defines the vertical scaling policy.
This parameter controls how the vertical scaler will prioritize usage and on-the-go instance type modifications.

By default, no vertical scaling is configured meaning all instances whatever their instance type or launch model (Spot vs
On-Demand) are handled the same way. 
                         
This parameter is a [MetaStringList](#MetaStringList)

    Ex: t3.medium,count=3,lighthouse;c5.large,spot;c5.large;c5.xlarge

Please consider reading [detailed decumentation about vertical scaling](SCALING.md) to ensure proper use.
                         """
                 },
                 "ec2.schedule.verticalscale.lighthouse_replacement_graceperiod" : "minutes=2",
                 "ec2.schedule.verticalscale.max_instance_type_modified_per_batch": 5,
                 "ec2.schedule.verticalscale.lighthouse_disable,Stable": {
                         "DefaultValue": 0,
                         "Format"      : "Bool",
                         "Description" : """Completly disable the LightHouse instance algorithm. 

As consequence, all instances matching the 'LightHouse' directive won't be scheduled.

Typical usage for this key is to ensure the fleet always run with best performing instances. As a example, Users could consider to use this 
key in combination with the instance scheduler to force the fleet to be 'LightHouse' instance free on peak load hours.
                         """
                 },
                 "ec2.schedule.bounce_delay,Stable" : {
                         "DefaultValue": 'minutes=0',
                         "Format"      : "Duration",
                         "Description" : """Max instance running time before bouncing.

By default, the bouncing algorithm is disabled. When this key is defined with a duration greated than 0 second, the fleet instances
are monitored for maximum age. Ex: 'days=2' means that, a instance running for more than 2 days will be bounced implying 
a fresh one will be started before the too old one is going to be stoppped.

Activating bouncing algorithm is a good way to keep the fleet correctly balanced from an AZ spread PoV and, if vertical scaling
is enabled, from an instance type distribution PoV.

> If your application supports it, activate instance bouncing.

                         """
                 },
                 "ec2.schedule.bounce_instance_jitter" : 'minutes=10',
                 "ec2.schedule.bounce_instance_cooldown" : 'minutes=10',
                 "ec2.schedule.bounce.instances_with_issue_grace_period": "minutes=5",
                 "ec2.schedule.draining.instance_cooldown": "minutes=2",
                 "ec2.schedule.start.warmup_delay": "minutes=2",
                 "ec2.schedule.burstable_instance.max_cpu_credit_unhealthy_instances,Stable" : {
                     "DefaultValue" : "1",
                     "Format"       : "IntegerOrPercentage",
                     "Description"  : """Maximum number of instances that could be considered, at a given time, as unhealthy because their CPU credit is exhausted.

* Setting this parameter to `100%` will indicate that all burstable instances could marked as unhealthy at the same time.
* Setting this parameter to `0` will completely disable the ability to consider burstable instances as unhealthy. 

> To prevent a DDoS, burstable instances with exhausted CPU Credit balance are NOT marked as unhealthy when len(stopped_instance) - (ec2.scheduler.min_instance_count) <= 0.
                     """
                 },
                 "ec2.schedule.burstable_instance.max_cpu_crediting_instances,Stable" : {
                     "DefaultValue" : "50%",
                     "Format"       : "IntegerOrPercentage",
                     "Description"  : """Maximum number of instances that could be in the CPU crediting state at the same time.

Setting this parameter to 100% could lead to fleet availability issues and so is not recommended. Under scaleout stress
condition, CloneSquad will automatically stop and restart instances in CPU Crediting state but it may take time (up to 3 mins). 
                     
    If you need to increase this value, it may mean that your burstable instance types are too 
    small for your workload. Consider upgrading instance types instead.
                     """
                 },
                 "ec2.schedule.burstable_instance.preserve_accrued_cpu_credit,Stable": {
                         "DefaultValue": 1,
                         "Format"      : "Bool",
                         "Description" : """Enable the weekly wakeup of burstable instances ["t3","t4"]

This flag enables an automatic wakeup of stopped instances before the one-week limit meaning accrued CPU Credit loss.
                         """
                 },
                 "ec2.schedule.burstable_instance.max_time_stopped": "days=6,hours=12",
                 "ec2.schedule.burstable_instance.max_cpucrediting_time,Stable": {
                         "DefaultValue": "hours=12",
                         "Format"      : "Duration",
                         "Description" : """Maximum duration that an instance can spent in the 'CPU Crediting' state.

This parameter is a safety guard to avoid a burstable instance with a faulty high-cpu condition to induce 'unlimited' credit
over spending for ever.
                        """
                 },
                 "ec2.schedule.burstable_instance.min_cpu_credit_required,Stable": {
                         "DefaultValue" : "30%",
                         "Format"       : "IntegerOrPercentage",
                         "Description"  : """The minimun amount CPU Credit that a burstable instance should have before to be shutdown.

This value is used by the 'CPU Crediting' algorithm to determine when a Burstable instance has gained enough credits to leave the
CPU Crediting mode and be shutdown.

When specified in percentage, 100% represents the ['Maximum earned credits than be accrued in a single day'](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html).

    Ex: `30%` for a t3.medium means a minimum cpu credit of `0.3 * 576 = 172`

                         """
                 },
                 "ec2.schedule.metrics.time_resolution": "60",
                 "ec2.schedule.spot.min_stop_period": {
                         "DefaultValue": "minutes=4",
                         "Format"      : "Duration",
                         "Description" : """Minimum duration a Spot instance needs to spend in 'stopped' state.

A very recently stopped persistent Spot instance can not be restarted immediatly for technical reasons. This 
parameter should NOT be modified by user.
                         """
                 },
                 "cloudwatch.staticfleet.use_dashboard,Stable": {
                         "DefaultValue": "1",
                         "Format": "Bool",
                         "Description": """Enable or disabled the dashboard dedicated to Subfleets.

By default, the dashboard is enabled.

> Note: The dashboard is configured only if there is at least one Subfleet with detailed metrics.
                 """},
                 "ec2.schedule.verticalscale.disable_instance_type_plannning": 0
        })

        self.state_ttl = Cfg.get_duration_secs("ec2.schedule.state_ttl")

        self.metric_time_resolution = Cfg.get_int("ec2.schedule.metrics.time_resolution")
        if self.metric_time_resolution < 60: metric_time_resolution = 1 # Switch to highest resolution

        self.cloudwatch.register_metric([
                { "MetricName": "DrainingInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "RunningInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "PendingInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "StoppedInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "StoppingInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfInstanceInInitialState",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfInstanceInUnuseableState",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfBouncedInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfExcludedInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfCPUCreditExhaustedInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfCPUCreditingInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "InstanceScaleScore",
                  "Unit": "None",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "FleetSize",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "MinInstanceCount",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "DesiredInstanceCount",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "NbOfInstancesInError",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "RunningLighthouseInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "FleetvCPUCount",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "FleetvCPUNeed",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "FleetMemCount",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "FleetMemNeed",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "StaticFleet.EC2.Size",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "StaticFleet.EC2.RunningInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "StaticFleet.EC2.DrainingInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
            ])

        self.ec2.register_state_aggregates([
            {
                "Prefix": "ec2.schedule.instance.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("ec2.schedule.state_ttl")
            }
            ])



    def get_prerequisites(self):
        self.cpu_credits = yaml.safe_load(str(misc.get_url("internal:cpu-credits.yaml"),"utf-8"))
        self.ec2_alarmstate_table   = kvtable.KVTable(self.context, self.context["AlarmStateEC2Table"])
        self.ec2_alarmstate_table.reread_table()

        # The scheduler part is making an extensive use of filtered/sorted lists that could become
        #   cpu and time consuming to build. We build here a library of filtered/sorted lists 
        #   available to all algorithms.
        # Library of filtered/sorted lists excluding the 'excluded' instances
        xray_recorder.begin_subsegment("prerequisites:prepare_instance_lists")
        log.debug("Computing all instance lists needed for scheduling")
        cache = {} # Avoid to compute many times the same thing by providing a cache 
        self.instances_wo_excluded                                = self.ec2.get_instances(cache=cache, ScalingState="-excluded")
        self.instances_wo_excluded_error                          = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, ScalingState="-error")
        self.running_instances_wo_excluded                        = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="running")
        self.pending_instances_wo_draining_excluded               = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="pending", ScalingState="-draining")
        self.stopped_instances_wo_excluded                        = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="stopped")
        self.stopped_instances_wo_excluded_error                  = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="stopped", ScalingState="-error")
        self.stopping_instances_wo_excluded                       = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="stopping")
        self.pending_running_instances_draining_wo_excluded       = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="pending,running", ScalingState="draining")
        self.pending_running_instances_bounced_wo_excluded        = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="pending,running", ScalingState="bounced")
        self.pending_running_instances_wo_excluded                = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="pending,running")
        self.pending_running_instances_wo_excluded_draining_error = self.ec2.get_instances(cache=cache, instances=self.instances_wo_excluded, State="pending,running", ScalingState="-draining,error")

        # Useable and serving instances
        self.compute_spot_exclusion_lists()
        self.initializing_instances                 = self.get_initial_instances()
        self.cpu_exhausted_instances                = self.get_cpu_exhausted_instances()
        self.instances_with_issues                  = self.get_instances_with_issues()
        self.useable_instances                      = self.get_useable_instances()
        self.useable_instances_wo_excluded_draining = self.get_useable_instances(instances=self.instances_wo_excluded, ScalingState="-draining")
        self.serving_instances                      = self.get_useable_instances(exclude_initializing_instances=True)

        # LightHouse filtered/sorted lists
        self.lighthouse_instances_wo_excluded_ids     = self.get_lighthouse_instance_ids(self.instances_wo_excluded)
        self.draining_lighthouse_instances_ids        = self.get_lighthouse_instance_ids(instances=self.pending_running_instances_draining_wo_excluded)
        self.serving_lighthouse_instances_ids         = self.get_lighthouse_instance_ids(self.serving_instances)
        self.useable_lighthouse_instance_ids          = self.get_lighthouse_instance_ids(self.useable_instances)
        self.serving_non_lighthouse_instance_ids              = list(filter(lambda i: i["InstanceId"] not in self.lighthouse_instances_wo_excluded_ids, self.useable_instances_wo_excluded_draining))
        self.serving_non_lighthouse_instance_ids_initializing = list(filter(lambda i: i["InstanceId"] not in self.useable_lighthouse_instance_ids, self.get_useable_instances(initializing_only=True)))
        self.lh_stopped_instances_wo_excluded_error   = self.get_lighthouse_instance_ids(instances=self.stopped_instances_wo_excluded_error)

        # Other filtered/sorted lists
        self.stopped_instances_wo_excluded_error    = self.ec2.get_instances(cache=cache, State="stopped", ScalingState="-excluded,error")
        self.pending_running_instances_draining     = self.ec2.get_instances(cache=cache, State="pending,running", ScalingState="draining")
        self.excluded_instances                     = self.ec2.get_instances(cache=cache, ScalingState="excluded")
        self.error_instances                        = self.ec2.get_instances(cache=cache, ScalingState="error")
        self.non_burstable_instances                = self.ec2.get_non_burstable_instances()
        self.stopped_instances_bounced_draining     = self.ec2.get_instances(cache=cache, State="stopped", ScalingState="bounced,draining")

        # Static fleet
        self.static_subfleet_instances              = self.ec2.get_static_subfleet_instances()
        self.running_static_subfleet_instances      = self.ec2.get_instances(cache=cache, instances=self.static_subfleet_instances, State="running")
        self.draining_static_subfleet_instances     = self.ec2.get_instances(cache=cache, instances=self.static_subfleet_instances, ScalingState="draining")
        log.debug("End of instance list computation.")
        xray_recorder.end_subsegment()


        # Garbage collect incorrect statuses (can happen when user stop the instance 
        #   directly on console
        instances = self.stopped_instances_bounced_draining 
        for i in instances:
            instance_id = i["InstanceId"]
            log.debug("Garbage collection instance '%s' with improper 'draining' status..." % instance_id)
            self.ec2.set_scaling_state(instance_id, "")
        # Garbage collect zombie states (i.e. instances do not exist anymore but have still states
        instances = self.ec2.get_instances() 
        for state in self.ec2.list_states(not_matching_instances=instances):
            log.debug("Garbage collect key '%s'..." % state)
            self.ec2.set_state(state, "", TTL=1)

        # Display a warning if some AZ are manually disabled
        disabled_azs = self.get_disabled_azs()
        if len(disabled_azs) > 0:
            log.info("Some Availability Zones are disabled: %s" % disabled_azs)

        # Register dynamic keys for subfleets
        for subfleet in self.ec2.get_static_subfleet_names():
            extended_metrics = Cfg.get_int("staticfleet.%s.ec2.schedule.metrics.enable" % subfleet) 
            log.log(log.NOTICE, "Enabled detailed metrics for subfleet '%s'." % subfleet)
            if extended_metrics:
                dimensions = [{
                    "Name": "SubfleetName",
                    "Value": subfleet}]
                self.cloudwatch.register_metric([ 
                        { "MetricName": "StaticFleet.EC2.Size",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "StaticFleet.EC2.RunningInstances",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "StaticFleet.EC2.DrainingInstances",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                    ])



    ###############################################
    #### EVENT GENERATION #########################
    ###############################################

    @xray_recorder.capture()
    def generate_instance_transition_events(self):
        #  Generate events on instance state transition 
        transitions = []
        for instance in self.instances_wo_excluded: #ec2.get_instances(ScalingState="-excluded"):
            instance_id    = instance["InstanceId"]
            previous_state = self.ec2.get_state("ec2.schedule.instance.last_known_state.%s" % instance_id)
            if previous_state is None: previous_state = "None"
            current_state = instance["State"]["Name"]
            if current_state != previous_state:
                transitions.append({
                        "InstanceId": instance_id,
                        "PreviousState" : previous_state,
                        "NewState": current_state
                    })
            self.set_state("ec2.schedule.instance.last_known_state.%s" % instance_id, current_state)
        if len(transitions):
            R(None, self.instance_transitions, Transitions=transitions)

    def instance_transitions(self, Transitions=None):
        return {}



    ###############################################
    #### UTILITY FUNCTIONS ########################
    ###############################################

    def get_min_instance_count(self):
        instances = self.instances_wo_excluded # ec2.get_instances(ScalingState="-excluded")
        c         = Cfg.get_abs_or_percent("ec2.schedule.min_instance_count", -1, len(instances))
        return c if c > 0 else 0

    def desired_instance_count(self):
        instances = self.instances_wo_excluded # ec2.get_instances(ScalingState="-excluded")
        return Cfg.get_abs_or_percent("ec2.schedule.desired_instance_count", -1, len(instances))

    def get_instances_with_issues(self):
        active_instances        = self.pending_running_instances_wo_excluded #ec2.get_instances(State="pending,running", ScalingState="-excluded")
        instances_with_issue_ids= []

        # TargetGroup related issues
        instances_with_issue_ids.extend(self.targetgroup.get_registered_instance_ids(state="unavail,unhealthy"))

        # EC2 related issues
        impaired_instances      = [ i["InstanceId"] for i in active_instances if self.ec2.is_instance_state(i["InstanceId"], ["impaired", "unhealthy", "az_evicted"]) ]
        [ instances_with_issue_ids.append(i) for i in impaired_instances if i not in instances_with_issue_ids]

        # Interrupted spot instances are 'unhealthy' too
        for i in self.spot_excluded_instance_ids:
            instance = self.ec2.get_instance_by_id(i)
            if instance["State"]["Name"] == "running":
                instances_with_issue_ids.append(i)

        # CPU credit "issues"
        exhausted_cpu_instances = sorted([ i["InstanceId"] for i in self.cpu_exhausted_instances])
        all_instances          = self.instances_wo_excluded_error # ec2.get_instances(ScalingState="-excluded,error")
        max_i                   = len(all_instances)
        for i in exhausted_cpu_instances:
            if max_i <= 0 or i in instances_with_issue_ids:
                continue
            instances_with_issue_ids.append(i)
            max_i -= 1

        return instances_with_issue_ids

    def get_useable_instances(self, instances=None, State="pending,running", ScalingState=None,
            exclude_problematic_instances=True, exclude_bounced_instances=True, 
            exclude_initializing_instances=False, initializing_only=False):
        if ScalingState is None: ScalingState = "-excluded,draining,error%s" % (",bounced" if exclude_bounced_instances else "")
        active_instances = self.ec2.get_instances(instances=instances, State=State, ScalingState=ScalingState)
        
        instances_ids_with_issues   = self.instances_with_issues # get_instances_with_issues()

        if exclude_problematic_instances:
            active_instances = list(filter(lambda i: i["InstanceId"] not in instances_ids_with_issues, active_instances))

        if initializing_only:
            active_instances = list(filter(lambda i: i["InstanceId"] in self.initializing_instances, active_instances))

        if exclude_initializing_instances:
            active_instances = list(filter(lambda i: i["InstanceId"] not in self.initializing_instances, active_instances))

        return active_instances

    def get_useable_instance_count(self, exclude_problematic_instances=True, exclude_bounced_instances=True, 
            exclude_initializing_instances=False, initializing_only=False):
        return len(self.get_useable_instances(exclude_problematic_instances=exclude_problematic_instances, 
                exclude_bounced_instances=exclude_bounced_instances, exclude_initializing_instances=exclude_initializing_instances,
                initializing_only=initializing_only))

    def get_cpu_exhausted_instances(self, threshold=1):
        max_cpu_credit_unhealthy      = Cfg.get_int("ec2.schedule.burstable_instance.max_cpu_credit_unhealthy_instances")
        # When not enough stopped instances are available, we do not mark unhealthy burstable instances to avoid fleet size exhaustion and a DDoS
        stopped_instance_margin       = max(0, len(self.stopped_instances_wo_excluded_error) - self.get_min_instance_count())
        max_issues                    = min(max_cpu_credit_unhealthy, stopped_instance_margin)
        instances_unhealthy           = []
        instances_should_be_unhealthy = []
        for i in self.pending_running_instances_wo_excluded_draining_error: 
            instance_type           = i["InstanceType"]
            cpu_credit = self.ec2.get_cpu_creditbalance(i)
            if cpu_credit >= 0 and cpu_credit <= threshold:
                if len(instances_unhealthy) < max_issues:
                    instances_unhealthy.append(i)
                else:
                    instances_should_be_unhealthy.append(i)
        if len(instances_unhealthy) + len(instances_should_be_unhealthy):
            info = ("Instances %s should also be set unhealthy but max unhealthy instance count is shapped to '%s' "
                    "[=min(ec2.schedule.burstable_instance.max_cpu_credit_unhealthy_instances=%s, stopped_instance_count-min_instance_count=%s)]"
                    % (self.ec2.get_instance_ids(instances_should_be_unhealthy), max_issues, 
                        max_cpu_credit_unhealthy, stopped_instance_margin))
            log.info("Burstable instances %s considered as unhealthy due to CPU Credit Balance exhausted. %s" %
                (self.ec2.get_instance_ids(instances_unhealthy), info if len(instances_should_be_unhealthy) else "")) 
        return instances_unhealthy

    def get_young_instance_ids(self):
        now                     = self.context["now"]
        warmup_delay            = Cfg.get_duration_secs("ec2.schedule.start.warmup_delay")
        active_instances        = self.pending_running_instances_wo_excluded # ec2.get_instances(State="pending,running", ScalingState="-excluded")
        return [ i["InstanceId"] for i in active_instances if (now - i["LaunchTime"]).total_seconds() < warmup_delay]

    def get_initial_instances(self):
        active_instances        = self.pending_running_instances_wo_excluded # ec2.get_instances(State="pending,running", ScalingState="-excluded")
        initializing_instances  = []
        initializing_instances.extend([ i["InstanceId"] for i in active_instances if self.ec2.is_instance_state(i["InstanceId"], ["initializing"]) ])
        initializing_instances.extend(self.targetgroup.get_registered_instance_ids(state="initial"))
        young_instances         = self.get_young_instance_ids()
        return list(filter(lambda i: i["InstanceId"] in young_instances or i["InstanceId"] in initializing_instances, active_instances))

    def get_initial_instances_ids(self):
        return [i["InstanceId"] for i in self.initializing_instances]

    def get_disabled_azs(self):
        return self.ec2.get_azs_with_issues()

    def set_state(self, key, value, TTL=None):
        if TTL is None: TTL=self.state_ttl
        self.ec2.set_state(key, value, TTL=TTL)


    ###############################################
    #### MAIN MODULE ENTRYPOINT ###################
    ###############################################

    @xray_recorder.capture()
    def schedule_instances(self):
        """
        This is the function that manage all decisions related to scaling
        """
        self.generate_instance_transition_events()
        self.manage_spot_events()
        self.compute_instance_type_plan()
        self.shelve_extra_lighthouse_instances()
        self.scale_desired()
        self.scale_bounce()
        self.scale_bounce_instances_with_issues()
        self.scale_in_out()
        self.wakeup_burstable_instances()
        self.manage_static_subfleets()
        self.generate_static_subfleet_dashboard()

    @xray_recorder.capture()
    def prepare_metrics(self):
        # Update statistics
        cw = self.cloudwatch
        fleet_instances        = self.instances_wo_excluded
        draining_instances     = self.pending_running_instances_draining_wo_excluded
        running_instances      = self.running_instances_wo_excluded
        pending_instances      = self.pending_instances_wo_draining_excluded
        stopped_instances      = self.stopped_instances_wo_excluded
        stopping_instances     = self.stopping_instances_wo_excluded
        excluded_instances     = self.excluded_instances
        bounced_instances      = self.pending_running_instances_bounced_wo_excluded
        error_instances        = self.error_instances
        exhausted_cpu_credits  = self.cpu_exhausted_instances
        instances_with_issues  = self.instances_with_issues
        static_subfleet_instances         = self.static_subfleet_instances
        running_static_subfleet_instances = self.running_static_subfleet_instances
        draining_static_subfleet_instances = self.draining_static_subfleet_instances
        fl_size                = len(fleet_instances)
        cw.set_metric("FleetSize",             len(fleet_instances) if fl_size > 0 else None)
        cw.set_metric("DrainingInstances",     len(draining_instances) if fl_size > 0 else None)
        cw.set_metric("RunningInstances",      len(running_instances) if fl_size > 0 else None)
        cw.set_metric("PendingInstances",      len(pending_instances) if fl_size > 0 else None)
        cw.set_metric("StoppedInstances",      len(stopped_instances) if fl_size > 0 else None)
        cw.set_metric("StoppingInstances",     len(stopping_instances) if fl_size > 0 else None)
        cw.set_metric("MinInstanceCount",      self.get_min_instance_count() if fl_size > 0 else None)
        cw.set_metric("DesiredInstanceCount",  max(self.desired_instance_count(), 0) if fl_size > 0 else None)
        cw.set_metric("NbOfExcludedInstances", len(excluded_instances) - len(static_subfleet_instances))
        cw.set_metric("NbOfBouncedInstances",  len(bounced_instances) if fl_size > 0 else None)
        cw.set_metric("NbOfInstancesInError",  len(error_instances) if fl_size > 0 else None)
        cw.set_metric("InstanceScaleScore",    self.instance_scale_score if fl_size > 0 else None)
        cw.set_metric("RunningLighthouseInstances", len(self.get_lighthouse_instance_ids(running_instances)) if fl_size > 0 else None)
        cw.set_metric("NbOfInstanceInInitialState", len(self.get_initial_instances()) if fl_size > 0 else None)
        cw.set_metric("NbOfInstanceInUnuseableState", len(instances_with_issues) if fl_size > 0 else None)
        cw.set_metric("NbOfCPUCreditExhaustedInstances", len(exhausted_cpu_credits))
        if len(static_subfleet_instances):
            # Send metrics only if there are Static fleet instances
            cw.set_metric("StaticFleet.EC2.Size", len(static_subfleet_instances))
            cw.set_metric("StaticFleet.EC2.RunningInstances", len(running_static_subfleet_instances))
            cw.set_metric("StaticFleet.EC2.DrainingInstances", len(draining_static_subfleet_instances))
        else:
            cw.set_metric("StaticFleet.EC2.Size", None)
            cw.set_metric("StaticFleet.EC2.RunningInstances", None)
            cw.set_metric("StaticFleet.EC2.DrainingInstances", None)

        # vCPU + Mem need estimations
        serving_instances = self.ec2.get_instances(self.pending_running_instances_wo_excluded_draining_error, ScalingState="-bounced")
        if len(serving_instances) and "_InstanceType" in serving_instances[0]:
            fleet_vcpu_count = sum(i["_InstanceType"]["VCpuInfo"]["DefaultVCpus"] for i in serving_instances)
            fleet_mem_count  = sum(i["_InstanceType"]["MemoryInfo"]["SizeInMiB"]  for i in serving_instances)
            fleet_vcpu_need  = int(fleet_vcpu_count * self.integrated_raw_instance_scale_score * 100) / 100.0
            fleet_mem_need   = int(fleet_mem_count  * self.integrated_raw_instance_scale_score * 100) / 100.0
            log.info("Current Fleet resources: TotalvCPU=%s, TotalMem=%s MiB, vCPUNeed=%s, MemNeed=%s MiB" %
                    (fleet_vcpu_count, fleet_mem_count, fleet_vcpu_need, fleet_mem_need))
            cw.set_metric("FleetvCPUCount", fleet_vcpu_count)
            cw.set_metric("FleetMemCount", fleet_mem_count)
            cw.set_metric("FleetvCPUNeed", fleet_vcpu_need)
            cw.set_metric("FleetMemNeed", fleet_mem_need)
        else:
            cw.set_metric("FleetvCPUCount", None)
            cw.set_metric("FleetMemCount", None)
            cw.set_metric("FleetvCPUNeed", None)
            cw.set_metric("FleetMemNeed", None)

        if len(error_instances):
            log.info("These instances are in 'ERROR' state: %s" % [i["InstanceId"] for i in error_instances])
        if len(instances_with_issues): 
            log.info("These instances are 'unuseable' (unavail/unhealthy/impaired/spotinterrupted/lackofcpucredit...) : %s" % instances_with_issues)

        # Compute higher level synthethic metrics
        running_fleet_size       = len(running_instances)
        serving_fleet_size       = self.get_useable_instance_count(exclude_problematic_instances=True) 
        maximum_fleet_size       = len(self.instances_wo_excluded_error_spotexcluded)
        managed_fleet_size       = len(self.instances_wo_excluded)
        servingfleetpercentage_maximumfleetsize = int(min(100, 100 * serving_fleet_size / maximum_fleet_size)) if maximum_fleet_size else 0
        servingfleetpercentage_managedfleetsize = int(min(100, 100 * serving_fleet_size / managed_fleet_size)) if managed_fleet_size else 0
        s_metrics = {
            "AutoscaledFleet" : {
                "MaximumFleetSize"                            : maximum_fleet_size,
                "ManagedFleetSize"                            : managed_fleet_size,
                "UnhealthyFleetSize"                          : managed_fleet_size - maximum_fleet_size,
                "ServingFleetSize"                            : serving_fleet_size,
                "RunningFleetSize"                            : running_fleet_size,
                "ServingFleet_vs_MaximumFleetSizePourcentage" : servingfleetpercentage_maximumfleetsize,
                "ServingFleet_vs_ManagedFleetSizePourcentage" : servingfleetpercentage_managedfleetsize
            },
            "StaticSubfleets" : []
        }
        subfleet_stats = s_metrics["StaticSubfleets"]
        for subfleet in self.ec2.get_static_subfleet_names():
            stats = {
                    "Name": subfleet,
                    "RunningInstances": [],
                    "RunningInstanceCount": 0,
                    "StoppedInstances": [],
                    "StoppedInstanceCount": 0,
                    "SubfleetSize": 0
                }
            fleet = self.ec2.get_static_subfleet_instances(subfleet_name=subfleet)
            for i in fleet:
                instance_id    = i["InstanceId"]
                instance_state = i["State"]["Name"]
                if instance_state in ["pending", "running"]:
                    stats["RunningInstances"].append(instance_id)
                if instance_state in ["stopped"]:
                    stats["StoppedInstances"].append(instance_id)
            stats["RunningInstanceCount"] = len(stats["RunningInstances"])
            stats["StoppedInstanceCount"] = len(stats["StoppedInstances"])
            stats["SubfleetSize"]         = len(fleet)
            subfleet_stats.append(stats)
        self.synthetic_metrics = s_metrics

    def get_synthetic_metrics(self):
        return self.synthetic_metrics

    ###############################################
    #### LOW LEVEL INSTANCE HANDLING ##############
    ###############################################

    def filter_stopped_instance_candidates(self, active_instances, stopped_instances):
        # Filter out Spot instance types that we know under interruption or close to interruption
        candidate_instances = self.ec2.filter_spot_instances(stopped_instances, filter_out_instance_types=self.excluded_spot_instance_types)
        if len(candidate_instances) != len(stopped_instances):
            if len(candidate_instances):
                # We allow Spot filtering only if other kinds of instance are eligible. If not, we try anyway our chance 
                #   with Spot marked as at risk of interruption...
                log.info("Filtered %d Spot instance(s) with type(s) '%s'!" % (len(stopped_instances) - len(candidate_instances), self.excluded_spot_instance_types))
                stopped_instances = candidate_instances
            elif len(stopped_instances):
                log.warning("All stopped instances are using Spot instance types (%s) marked for interruption!!"
                        "Please consider increase fleet instance type diversity!!" % self.excluded_spot_instance_types)

        # Filter out Spot instances recently stopped as we can't technically restart them before some time
        stopped_instances = self.ec2.filter_instance_recently_stopped(stopped_instances, 
                Cfg.get_duration_secs("ec2.schedule.spot.min_stop_period"), filter_only_spot=True)

        # Ensure we pick instances in a way that keep AZs balanced
        stopped_instances = self.ec2.sort_by_balanced_az(stopped_instances, active_instances, 
                smallest_to_biggest_az=True, excluded_instance_ids=self.instances_with_issues)
        return stopped_instances

    def filter_autoscaled_stopped_instance_candidates(self, caller, expected_instance_count, target_for_dispatch=None):
        """ Return a list of startable instances in the autoscaled fleet.
        """
        disabled_azs     = self.get_disabled_azs()
        active_instances = self.pending_running_instances_wo_excluded_draining_error 
        # Get all stopped instances
        stopped_instances = self.ec2.get_instances(State="stopped", azs_filtered_out=disabled_azs)

        # Get a list of startable instances
        stopped_instances = self.filter_stopped_instance_candidates(active_instances, stopped_instances)

        # Let the scaleout algorithm influence the instance selection
        stopped_instances = self.scaleup_sort_instances(stopped_instances, 
                target_for_dispatch if target_for_dispatch is not None else expected_instance_count, 
                caller)
        return stopped_instances

    def filter_running_instance_candidates(self, active_instances):
        """ Return a list of stoppable instances in the autoscaled fleet.
        """
        # Ensure we picked instances in a way that we keep AZs balanced
        active_instances = self.ec2.sort_by_balanced_az(active_instances, active_instances, smallest_to_biggest_az=False)

        # If we have disabled AZs, we placed them in front to remove associated instances first
        active_instances = self.ec2.sort_by_prefered_azs(active_instances, prefered_azs=self.get_disabled_azs()) 

        # We place instance with unuseable status in front of the list
        active_instances = self.ec2.sort_by_prefered_instance_ids(active_instances, prefered_ids=self.instances_with_issues) 

        # Put interruped Spot instances as first candidates to stop
        active_instances = self.ec2.filter_spot_instances(active_instances, EventType="+rebalance_recommended",
                filter_in_instance_types=self.excluded_spot_instance_types, merge_matching_spot_first=True)
        active_instances = self.ec2.filter_spot_instances(active_instances, EventType="+interrupted",
                filter_in_instance_types=self.excluded_spot_instance_types, merge_matching_spot_first=True)
        return active_instances


    @xray_recorder.capture()
    def instance_action(self, desired_instance_count, caller, reject_if_initial_in_progress=False, target_for_dispatch=None):
        """
        Perform action (start or stop instances) needed to achieve specified 'desired_instance_count'
        """
        now = self.context["now"]

        min_instance_count = self.get_min_instance_count()
        log.log(log.NOTICE, "Min required instance count : %d" % min_instance_count)

        is_scalein_caller = caller == "scalein" 
        useable_instances_count = self.get_useable_instance_count(exclude_problematic_instances=not is_scalein_caller) 
        log.log(log.NOTICE, "Number of useable instances : %d" % useable_instances_count)
        
        expected_instance_count  = max(desired_instance_count, min_instance_count)
        delta_instance_count    = expected_instance_count - useable_instances_count

        if delta_instance_count != 0: 
            log.debug("[INFO] instance_action (%d) from '%s'... " % (delta_instance_count, caller))

        if delta_instance_count > 0:
            max_instance_start_at_a_time = Cfg.get_int("ec2.schedule.max_instance_start_at_a_time")
            c = min(delta_instance_count, max_instance_start_at_a_time)

            stopped_instances = self.filter_autoscaled_stopped_instance_candidates(caller, expected_instance_count, target_for_dispatch=target_for_dispatch)
            
            instance_ids_to_start = self.ec2.get_instance_ids(stopped_instances)

            # Start selected instances
            if len(instance_ids_to_start) > 0:
                log.info("Starting up to %s (shaped to %d. See ec2.schedule.max_instance_start_at_a_time setting) instances..." % 
                        (delta_instance_count, c))

                self.ec2.start_instances(instance_ids_to_start, max_started_instances=c)
                self.scaling_state_changed = True

        if delta_instance_count < 0:
            # We need to assume that instance inserted in a target group and in a 'initial' state
            #    can't be stated as useable instances yet. We delay downsize decision
            if reject_if_initial_in_progress:
                initial_target_instance_ids = self.get_initial_instances_ids()
                log.log(log.NOTICE, "Number of instance target in 'initial' state: %d" % len(initial_target_instance_ids))
                if len(initial_target_instance_ids) > 0:
                    log.log(log.NOTICE, "Some targets are still initializing. Do not consider stopping instances now...")
                    return False

            c = min(-delta_instance_count, Cfg.get_int("ec2.schedule.max_instance_stop_at_a_time"))
            log.info("Draining up to %s (shapped to %d) instances..." % (-delta_instance_count, c))

            # Retrieve a list of running instances candidates for stop
            active_instances = self.filter_running_instance_candidates(self.pending_running_instances_wo_excluded_draining_error)

            # Take into account scaledown algorithm
            active_instances = self.scaledown_sort_instances(active_instances, 
                    target_for_dispatch if target_for_dispatch is not None else expected_instance_count, 
                    caller)
            
            instance_ids_to_drain = []
            for i in self.ec2.get_instance_ids(active_instances):
                if self.ec2.get_scaling_state(i) != "draining":
                    self.ec2.set_scaling_state(i, "draining")
                    self.scaling_state_changed = True
                    instance_ids_to_drain.append(i)
                    c -= 1
                if c == 0:
                    break
            # Send an event to interested users
            R(None, self.drain_instances, DrainedInstanceIds=instance_ids_to_drain)
        return True

    def drain_instances(self, DrainedInstanceIds=None):
        return {}

    def is_instance_cpu_crediting_eligible(self, i):
       instance_type = i["InstanceType"]
       if instance_type not in self.cpu_credits or instance_type.startswith("t2"):
           return False # Not a burstable or not eligible instance
       return True

    def is_instance_need_cpu_crediting(self, i, meta):
       now           = self.context["now"]
       instance_id   = i["InstanceId"]

       instance_type = i["InstanceType"]
       if not self.is_instance_cpu_crediting_eligible(i):
           return False 

       # This instance to stop is a burstable one
       stopped_instances    = self.stopped_instances_wo_excluded_error
       lighthouse_instances = self.lh_stopped_instances_wo_excluded_error

       # Burstable machine needs to run enough time to get their CPU Credit balance updated
       draining_date = meta["last_draining_date"]
       draining_time = misc.seconds_from_epoch_utc()
       if draining_date is not None:
           draining_time     = (now - draining_date).total_seconds()
       running_time          = (now - i["LaunchTime"]).total_seconds()

       assessment_time       = min(draining_time, running_time)
       maximum_draining_time = Cfg.get_duration_secs("ec2.schedule.burstable_instance.max_cpucrediting_time")
       if assessment_time > maximum_draining_time:
           log.warning("Instance '%s' is CPU crediting for a too long time. Timeout..." % instance_id)
           return False

       max_earned_credits      = self.cpu_credits[instance_type][1]
       min_cpu_credit_required = Cfg.get_abs_or_percent("ec2.schedule.burstable_instance.min_cpu_credit_required", -1, max_earned_credits)
       cpu_credit              = self.ec2.get_cpu_creditbalance(i)
       if cpu_credit == -1: 
           log.info("Waiting CPU Credit balance metric for instance %s..." % (instance_id))
           return True

       if self.ec2.get_state("ec2.schedule.debug.instance.%s.force_out_of_cpu_crediting" % instance_id) in ["True", "true"]:
           log.warn("Forced instance %s out of CPU Crediting state! (ec2.schedule.instance.%s.force_out_of_cpu_crediting=True)" % 
                   (instance_id, instance_id))
           self.set_state("ec2.schedule.debug.instance.%s.force_out_of_cpu_crediting" % instance_id, "False")
           return False

       if cpu_credit >= 0 and cpu_credit < min_cpu_credit_required:
           log.info("'%s' is CPU crediting... (is_static_fleet_instance=%s, current_credit=%.2f, minimum_required_credit=%s, maximum_possible_credit=%s, %s)" % 
                   (instance_id, self.ec2.is_static_subfleet_instance(instance_id), cpu_credit, min_cpu_credit_required, max_earned_credits, instance_type))
           return True
       return False

    @xray_recorder.capture()
    def stop_drained_instances(self):
        now              = self.context["now"]
        cw               = self.cloudwatch

        now = self.context["now"]
        draining_target_instance_ids = self.targetgroup.get_registered_instance_ids(state="draining")
        if len(draining_target_instance_ids):
            registered_targets = self.targetgroup.get_registered_targets(state="draining")
            log.debug(Dbg.pprint(registered_targets))

        # Retrieve list of instance marked as draining and running
        instances     = self.pending_running_instances_draining
        instances_ids = self.ec2.get_instance_ids(instances) 

        nb_cpu_crediting               = 0
        non_burstable_instances        = self.non_burstable_instances
        max_number_crediting_instances = Cfg.get_abs_or_percent("ec2.schedule.burstable_instance.max_cpu_crediting_instances", -1, 
                len(self.instances_wo_excluded))
        max_startable_stopped_instances= self.filter_autoscaled_stopped_instance_candidates("stop_drained_instances", len(self.useable_instances))
        # To avoid availability issues, we do not allow more cpu crediting instances than startable instances
        #    It ensures that burstable instances are still available *even CPU Exhausted* to the fleet.
        max_number_crediting_instances = min(max_number_crediting_instances, len(max_startable_stopped_instances) 
                + len(self.draining_lighthouse_instances_ids)) # We add the number of draining LH instances as they may be in CPU crediting state and
                                                               # can't be part of a full scaleout sequence (so are useless to be rendered available)
        ids_to_stop                    = []
        too_much_cpu_crediting         = False
        for i in instances:
           instance_id = i["InstanceId"]
           # Refresh the TTL if the 'draining' operation takes a long time
           meta = {}
           self.ec2.set_scaling_state(instance_id, "draining", meta=meta)

           need_stop_now = False
           # Special management for static subfleet instances. We need to check if each draining instance
           #   are marked for 'running' state. If True, we must shutdown this instance immediatly
           #   to allow its restart ASAP
           is_static_subfleet_instance = False
           if self.ec2.is_static_subfleet_instance(instance_id):
                subfleet_name               = self.ec2.get_static_subfleet_name_for_instance(i)
                is_static_subfleet_instance = True
                letter_box                  = self.letter_box_subfleet_to_stop_drained_instances
                # Did we receive a letter from subfleet management method?
                if letter_box[subfleet_name]:
                    need_stop_now              = True
                    letter_box[subfleet_name] -= 1
           
           if Cfg.get("ec2.schedule.desired_instance_count") == "100%":
               need_stop_now = True

           if not need_stop_now:
               if nb_cpu_crediting >= max_number_crediting_instances:
                   if not too_much_cpu_crediting and self.is_instance_cpu_crediting_eligible(i):
                        log.info("Maximum number of CPU Crediting instances reached! (nb_cpu_crediting=%s,ec2.schedule.max_cpu_crediting_instances=%d)" %
                           (nb_cpu_crediting, max_number_crediting_instances))
                        too_much_cpu_crediting = True
               elif self.is_instance_need_cpu_crediting(i, meta):
                   if is_static_subfleet_instance:
                       continue
                   else:
                       nb_cpu_crediting += 1
                       continue

               if instance_id in draining_target_instance_ids:
                   log.info("Can't stop yet instance %s. Target Group is still draining it..." % instance_id)
                   continue

               if self.targetgroup.is_instance_registered(None, instance_id):
                   log.log(log.NOTICE, "Instance if still part of a Target Group. Wait for eviction before to stop it...")
                   continue

               draining_date = meta["last_draining_date"]
               if draining_date is not None:
                   elapsed_time  = now - draining_date
                   cooldown      = Cfg.get_duration_secs("ec2.schedule.draining.instance_cooldown")
                   if elapsed_time < timedelta(seconds=cooldown):
                       log.info("Instance '%s' is still in draining cooldown period (elapsed_time=%d, ec2.schedule.draining.instance_cooldown=%d): "
                            "Do not assess stop now..." % (instance_id, elapsed_time.total_seconds(), cooldown))
                       continue
               need_stop_now = True

           if need_stop_now:
               ids_to_stop.append(instance_id)

        cw.set_metric("NbOfCPUCreditingInstances", nb_cpu_crediting)

        if len(ids_to_stop) > 0:
            if self.scaling_state_changed:
                log.debug("Instance state changed. Do stop instance stop now...")
                return

            self.ec2.stop_instances(ids_to_stop)
            log.info("Sent stop request for instances %s." % ids_to_stop)
            self.scaling_state_changed = True

    def wakeup_burstable_instances(self):
        """
        Start burstable instances that are stopped for a long time and could lose their CPU Credits soon.
        """
        if not Cfg.get_int("ec2.schedule.burstable_instance.preserve_accrued_cpu_credit"):
            log.log(log.NOTICE, "Burstable instance CPU Credit preservation disabled (ec2.schedule.burstable_instance.preserve_accrued_cpu_credit=0).")
            return

        now                = self.context["now"]
        stopped_instances  = self.stopped_instances_wo_excluded_error # ec2.get_instances(State="stopped", ScalingState="-excluded,error")
        for i in stopped_instances:
            if i["InstanceType"].startswith("t2") or not i["InstanceId"].startswith("t"):
                continue # T2 can't preserve their CPU credits
            instance_id    = i["InstanceId"]
            last_stop_date = self.ec2.instance_last_stop_date(instance_id, default=None)
            if last_stop_date is None:
                continue
            if (now - last_stop_date).total_seconds() > Cfg.get_duration_secs("ec2.schedule.burstable_instance.max_time_stopped"):
                log.info("Starting stopped too long burstable instance '%s' to preserve its CPU Credits" % instance_id)
                self.ec2.start_instances([instance_id])
                self.scaling_state_changed = True

    def manage_static_subfleets(self):
        """Manage start/stop actions for static subfleet instances
        """
        instances = self.static_subfleet_instances
        subfleets = {}
        for i in instances:
            instance_id    = i["InstanceId"]
            subfleet_name  = self.ec2.get_static_subfleet_name_for_instance(i)
            forbidden_chars = "[ .]"
            if re.match(forbidden_chars, subfleet_name):
                log.warning("Instance '%s' contains invalid characters (%s)!! Ignore this instance..." % (instance_id, forbidden_chars))
                continue
            expected_state = Cfg.get("staticfleet.%s.state" % subfleet_name, none_on_failure=True)
            if expected_state is None:
                log.log(log.NOTICE, "Encountered a static fleet instance (%s) without state directive. Please set 'staticfleet.%s.state' configuration key..." % 
                        (instance_id, subfleet_name))
                continue
            log.debug("Manage static fleet instance '%s': subfleet_name=%s, expected_state=%s" % (instance_id, subfleet_name, expected_state))

            allowed_expected_states = ["running", "stopped", "undefined", ""]
            if expected_state not in allowed_expected_states:
                log.warning("Expected state '%s' for static subfleet '%s' is not valid : (not in %s!)" % (expected_state, subfleet_name, allowed_expected_states))
                continue

            if subfleet_name not in subfleets:
                subfleets[subfleet_name] = defaultdict(list)

            if expected_state == "running":
                subfleets[subfleet_name]["ToStart"].append(i)

            if (expected_state == "stopped" and i["State"]["Name"] == "running" 
                    and self.ec2.get_scaling_state(instance_id, do_not_return_excluded=True) != "draining"):
                subfleets[subfleet_name]["ToStop"].append(i)

            subfleets[subfleet_name]["All"].append(i)

        
        # Manage start/stop of 'running' subfleet
        cache = {}
        for subfleet in subfleets:
            fleet                  = subfleets[subfleet]
            fleet_instances        = fleet["All"]
            running_instances      = self.ec2.get_instances(cache=cache, instances=fleet_instances, 
                    State="pending,running", ScalingState="-error,draining,bounced")
            stopped_instances      = self.ec2.get_instances(cache=cache, instances=fleet_instances, State="stopped")
            draining_instances     = self.ec2.get_instances(cache=cache, instances=fleet_instances, State="draining")
            if len(fleet["ToStop"]):
                instance_ids = [i["InstanceId"] for i in fleet["ToStop"]]
                log.info("Draining static fleet instance(s) '%s'..." % instance_ids)
                for instance_id in instance_ids:
                    self.ec2.set_scaling_state(instance_id, "draining")

            if len(fleet["ToStart"]):
                desired_instance_count = max(0, Cfg.get_abs_or_percent("staticfleet.%s.ec2.schedule.desired_instance_count" % subfleet, 
                    len(fleet_instances), len(fleet_instances)))
                delta                  = desired_instance_count - len(running_instances)
                if delta > 0:
                    stopped_instances = self.filter_stopped_instance_candidates(running_instances, stopped_instances)
                    if len(stopped_instances):
                        instances_to_start = [ i["InstanceId"] for i in stopped_instances ]
                        if delta > len(instances_to_start):
                            # If we can't start the request number of instances, we set a letter box variable
                            #   to ask stop_drained_instances() to release immediatly this amount of 'draining' 
                            #   instances if possible
                            self.letter_box_subfleet_to_stop_drained_instances[subfleet] = delta - len(instances_to_start)
                        log.info("Starting up to %d static fleet instance(s) (fleet=%s)..." % (len(instances_to_start), subfleet))
                        self.ec2.start_instances(instances_to_start, max_started_instances=desired_instance_count)
                        self.scaling_state_changed = True
                if delta < 0:
                    running_instances = self.filter_running_instance_candidates(running_instances)
                    if len(running_instances):
                        instances_to_stop = [i["InstanceId"] for i in running_instances][:-delta]
                        log.info("Draining static fleet instance(s) '%s'..." % instances_to_stop)
                        for instance_id in instances_to_stop:
                            self.ec2.set_scaling_state(instance_id, "draining")

            extended_metrics = Cfg.get_int("staticfleet.%s.ec2.schedule.metrics.enable" % subfleet)
            dimensions = [{
                "Name": "SubfleetName",
                "Value": subfleet}]
            if extended_metrics:
                self.cloudwatch.set_metric("StaticFleet.EC2.Size", len(fleet["All"]), dimensions=dimensions)
                self.cloudwatch.set_metric("StaticFleet.EC2.RunningInstances", len(running_instances), dimensions=dimensions)
                self.cloudwatch.set_metric("StaticFleet.EC2.DrainingInstances", len(draining_instances), dimensions=dimensions)

    def generate_static_subfleet_dashboard(self):
        now                = self.context["now"]
        dashboard          = { "widgets": [] }
        static_subfleets   = self.ec2.get_static_subfleet_names()
        fleet_with_details = []
        for i in range(0, len(static_subfleets)):
            subfleet_name = static_subfleets[i]
            if not Cfg.get_int("staticfleet.%s.ec2.schedule.metrics.enable" % subfleet_name):
                continue
            fleet_with_details.append(subfleet_name)
            widget = {
                    "type": "metric",
                    "x": 0 if (i+1) % 2 else 12,
                    "y": int(i / 2) * 6,
                    "width": 12,
                    "height": 6,
                    "properties": {
                        "view": "timeSeries",
                        "stacked": False,
                        "metrics": [
                            [ "CloneSquad", "StaticFleet.EC2.Size", "GroupName", self.context["GroupName"], "SubfleetName", subfleet_name ],
                            [ ".", "StaticFleet.EC2.RunningInstances", ".", ".", ".", "." ],
                            [ ".", "StaticFleet.EC2.DrainingInstances", ".", ".", ".", "." ]
                        ],
                        "region": self.context["AWS_DEFAULT_REGION"],
                        "title": subfleet_name
                    }
                }
            dashboard["widgets"].append(widget)

        use_dashboard    =  Cfg.get_int("cloudwatch.staticfleet.use_dashboard") if len(dashboard["widgets"]) != 0 else False
        fingerprint      = "%s : %s : %s " % (sorted(fleet_with_details), use_dashboard, 
                True if now.minute % 15 else False) # Make the fingerprint change every 15 minutes
        last_fingerprint = self.ec2.get_state("cloudwatch.staticfleet.last_fingerprint")
        client = self.context["cloudwatch.client"]
        if fingerprint != last_fingerprint:
            if not use_dashboard:
                try:
                    client.delete_dashboards(
                            DashboardNames=[self._get_dashboard_name()]
                        )
                except: 
                    pass
            else:
                log.log(log.NOTICE, "Configuring Static Subfleet CloudWatch dashboard...")
                response = client.put_dashboard(
                        DashboardName="CloneSquad-%s-Subfleets" % self.context["GroupName"],
                        DashboardBody=Dbg.pprint(dashboard)
                    )
        self.set_state("cloudwatch.staticfleet.last_fingerprint", fingerprint)


    ###############################################
    #### SCALE DESIRED & SCALE BOUNCE ALGOS #######
    ###############################################

    @xray_recorder.capture()
    def scale_desired(self):
        """
        Take scale decisions based on Min/Desired criteria
        """
        if self.scaling_state_changed:
            return

        # Step 0) Compute number of instance to start
        desired_instance_count = self.desired_instance_count()
        log.debug("Desired instance count : % d" % desired_instance_count)

        minimal_instance_count    = max(desired_instance_count, self.get_min_instance_count())

        # If you specify a precise number of instances, we assume that he is requesting
        #   useable instances and so we have to take into account problematic instances
        active_instance_count   = len(self.useable_instances)
        expected_instance_count = max(minimal_instance_count, active_instance_count) if desired_instance_count == -1 else minimal_instance_count

        # Step 1) Ask for the desired instance count
        self.instance_action(expected_instance_count, "scale_desired", reject_if_initial_in_progress=True)

    
    def scale_bounce_is_draining_condition(self):
        scale_down_disabled = Cfg.get_int("ec2.schedule.scalein.disable") != 0
        return scale_down_disabled or self.instance_scale_score < Cfg.get_float("ec2.schedule.scalein.threshold_ratio")

    @xray_recorder.capture()
    def scale_bounce(self):
        """
        IF configured, bounce old out-of-date by spawning new ones
        """
        if self.scaling_state_changed:
            return

        now = self.context["now"]
        bounce_delay_delta             = timedelta(seconds=Cfg.get_duration_secs("ec2.schedule.bounce_delay"))
        bounce_instance_cooldown_delta = timedelta(seconds=Cfg.get_duration_secs("ec2.schedule.bounce_instance_cooldown"))

        instances = self.pending_running_instances_wo_excluded # ec2.get_instances(State="pending,running", ScalingState="-excluded")
        bouncing_ids = []
        if self.scale_bounce_is_draining_condition():
            most_recent_bouncing_action = None
            for i in instances:
                instance_id = i["InstanceId"]
                meta={}
                status = self.ec2.get_scaling_state(instance_id, meta=meta)
                bounce_instance_jitter = timedelta(seconds=random.randint(0, Cfg.get_duration_secs("ec2.schedule.bounce_instance_jitter")))

                if status == "bounced":
                    bouncing_ids.append(instance_id)
                    if meta["last_action_date"] is not None:
                        bounce_time = meta["last_action_date"]
                        if most_recent_bouncing_action is None or most_recent_bouncing_action < bounce_time: most_recent_bouncing_action = bounce_time 
                        if now - bounce_time < bounce_instance_cooldown_delta + bounce_instance_jitter:
                            continue
                    self.ec2.set_scaling_state(instance_id, "draining")
                    self.scaling_state_changed = True
            if len(bouncing_ids):
                log.debug("Instances %s are already marked for bouncing..." % bouncing_ids)

        if bounce_delay_delta.total_seconds() == 0:
            log.log(log.NOTICE, "Instance bouncing not configured.")
            return

        # User requested that the whole fleet to be up, so disable bouncing algorithm
        if Cfg.get("ec2.schedule.desired_instance_count") == "100%":
            log.info("Bouncing algorithm disabled because 'ec2.schedule.desired_instance_count' == 100% !")
            return

        initial_target_instance_ids = self.get_initial_instances_ids()
        if len(initial_target_instance_ids):
            log.log(log.NOTICE, "Some targets are still in 'initial' state: Delaying instance bouncing assessment...")
            return

        #fleet_instances        = self.instances_wo_excluded # ec2.get_instances(ScalingState="-excluded")
        if len(self.pending_running_instances_bounced_wo_excluded): # ec2.get_instances(instances=fleet_instances, State="pending,running", ScalingState="bounced")):
            log.log(log.NOTICE, "Some instances are already bouncing... Wait to finish this task before another bounce...")
            return


        to_bounce_instance_ids = []
        # Put in front of the instance list, instances with issues to bounce them first
        unuseable_instance_ids = self.instances_with_issues
        instances = self.ec2.sort_by_prefered_instance_ids(instances, prefered_ids=unuseable_instance_ids) 
        bounce_instance_jitter = timedelta(seconds=random.randint(0, Cfg.get_duration_secs("ec2.schedule.bounce_instance_jitter")))

        for i in instances:
            instance_id = i["InstanceId"]
            timegap = now - i["LaunchTime"]

            status = self.ec2.get_scaling_state(instance_id)

            # Mark instance 'bounced'
            if (status not in ["bounced", "draining", "error"] and timegap > bounce_delay_delta + bounce_instance_jitter
                    and len(to_bounce_instance_ids) == 0): #Note: We bounce only one instance at a time to avoid tempest of restarts
                instance_id = i["InstanceId"]
                log.info("Bounced instance '%s' (%s)..." % (instance_id, 
                    "oldest" if instance_id not in unuseable_instance_ids else "unuseable"))

                to_bounce_instance_ids.append(instance_id)
                # Mark instance as 'bounced' with a not too big TTL. If bouncing
                self.ec2.set_scaling_state(instance_id, "bounced")
                                                                                                      

        if len(to_bounce_instance_ids) == 0:
            return

        log.info("Bouncing of instances %s in progress..." % to_bounce_instance_ids)
        self.instance_action(self.get_useable_instance_count() + len(to_bounce_instance_ids), "scale_bounce")

    @xray_recorder.capture()
    def scale_bounce_instances_with_issues(self):
        """ Function responsible to bounce instances identified with issues after a 
            configurable amount of time.
        """
        if self.scaling_state_changed:
            return
        # User requested that the whole fleet to be up, so disable draining of faulty instances...
        if Cfg.get("ec2.schedule.desired_instance_count") == "100%":
            return

        now                   = self.context["now"]
        grace_period          = Cfg.get_duration_secs("ec2.schedule.bounce.instances_with_issue_grace_period")
        instances_with_issues = self.instances_with_issues
        for i in instances_with_issues:
            if self.ec2.get_scaling_state(i) == "draining":
                continue
            last_seen_date = self.ec2.get_state_date("ec2.schedule.bounce.instance_with_issues.%s" % i)
            if last_seen_date is None:
                self.ec2.set_state("ec2.schedule.bounce.instance_with_issues.%s" % i, now, TTL=grace_period * 2)
                last_seen_date = now
            else:
                self.ec2.set_state("ec2.schedule.bounce.instance_with_issues.%s" % i, last_seen_date, TTL=grace_period * 2)
            if (now - last_seen_date).total_seconds() > grace_period + (grace_period * random.random()):
                self.ec2.set_scaling_state(i, "draining")
                log.info("Bounced instance '%s' with issues as grace period expired!" % i)
        

    
    ###############################################
    #### CORE SCALEUP/DOWN ALGORITHM ##############
    ###############################################

    def get_lighthouse_instance_ids(self, instances):
        # Collect instances that are marked as LightHouse through a Tag
        ins = self.ec2.filter_instance_list_by_tag(instances, "clonesquad:lighthouse", ["True","true"])
        ids = [i["InstanceId"] for i in ins]

        # Collect instances that are declared as LightHouse through Vertical scaling
        cfg = Cfg.get_list_of_dict("ec2.schedule.verticalscale.instance_type_distribution")
        lighthouse_instance_types = [t["_"] for t in list(filter(lambda c: "lighthouse" in c and c["lighthouse"], cfg))]

        ins = list(filter(lambda i: i["InstanceType"] in lighthouse_instance_types, instances))
        for i in [i["InstanceId"] for i in ins]: 
            if i not in ids: ids.append(i)
        return ids

    def are_lighthouse_instance_disabled(self):
        #all_instances  = self.ec2.get_instances(ScalingState="-excluded")
        all_lh_ids     = self.lighthouse_instances_wo_excluded_ids # get_lighthouse_instances(all_instances)
        return (Cfg.get_int("ec2.schedule.verticalscale.lighthouse_disable") or
                  self.get_min_instance_count() > len(all_lh_ids))

    def are_all_non_lh_instances_started(self):
        all_instances              = self.instances_wo_excluded_error_spotexcluded
        lh_ids                     = self.lighthouse_instances_wo_excluded_ids
        useable_instances          = self.useable_instances
        non_lighthouse_instances   = list(filter(lambda i: i["InstanceId"] not in lh_ids, useable_instances))
        all_non_lighthouse_instances       = list(filter(lambda i: i["InstanceId"] not in lh_ids, all_instances))
        all_non_lighthouse_instances_count = len(all_non_lighthouse_instances)
        return all_non_lighthouse_instances_count == len(non_lighthouse_instances)

    def shelve_instance_dispatch(self, expected_count):
        desired_instance_count= self.desired_instance_count()
        min_instance_count    = self.get_min_instance_count()
        useable_instance_count= self.get_useable_instance_count()
        all_instances         = self.instances_wo_excluded_error_spotexcluded
        all_lh_ids            = self.lighthouse_instances_wo_excluded_ids
        serving_lh_ids        = self.serving_lighthouse_instances_ids
        running_lh_ids        = self.useable_lighthouse_instance_ids 
        non_lighthouse_instances              = self.serving_non_lighthouse_instance_ids 
        non_lighthouse_instances_initializing = self.serving_non_lighthouse_instance_ids_initializing 
        serving_non_lighthouse_instance_count = len(non_lighthouse_instances) - len(non_lighthouse_instances_initializing)


        target_count             = max(max(desired_instance_count, min_instance_count), expected_count)
        recommended_amount_of_lh = max(min_instance_count - max(target_count - min_instance_count, 0), 0)

        if self.are_lighthouse_instance_disabled(): 
            recommended_amount_of_lh = 0

        if desired_instance_count != -1:
            # Special case for desired_instance_count != -1 where it is authorized to launch LightHouse instances
            #    when non-lighthouse amount is exhausted
            delta_lh = len(all_instances) - desired_instance_count
            if delta_lh < len(all_lh_ids):
                recommended_amount_of_lh = len(all_lh_ids) -  delta_lh
        recommended_amount_of_non_lh = target_count - recommended_amount_of_lh
        return [recommended_amount_of_lh, recommended_amount_of_non_lh]



    def _match_spot(self, i, c, spot_implicit=None):
        cc = c.copy()
        if spot_implicit is not None and "spot" not in cc:
            cc["spot"] = spot_implicit
        if "spot" in cc:
            is_spot = self.ec2.is_spot_instance(i)
            if cc["spot"] and not is_spot: return False
            if not cc["spot"] and is_spot: return False
        return True

    def scaleup_sort_instances(self, candidates, expected_count, caller):
        cfg    = Cfg.get_list_of_dict("ec2.schedule.verticalscale.instance_type_distribution")

        lh_ids = self.get_lighthouse_instance_ids(candidates)

        instances                  = []
        useable_instances          = self.useable_instances
        non_lighthouse_instances   = list(filter(lambda i: i["InstanceId"] not in lh_ids, useable_instances))
        min_instance_count         = self.get_min_instance_count()
        running_lh_ids             = self.get_lighthouse_instance_ids(useable_instances)

        lighthouse_need     = 0
        lighthouse_only     = False
        lighthouse_disabled = False

        delta_min_instance_count = len(useable_instances) - self.get_min_instance_count()
        if delta_min_instance_count < 0: lighthouse_need = -delta_min_instance_count
        amount_of_lh, amount_of_non_lh = self.shelve_instance_dispatch(expected_count)
        if self.are_lighthouse_instance_disabled():
            lighthouse_need = 0
            lighthouse_disabled = True
        else:
            lighthouse_need = max(amount_of_lh - len(running_lh_ids), 0)

        if caller in ["shelve"]:
            lighthouse_only = True

        if caller in ["scale_bounce"]:
            # Bounce algorithm wants a new instance!
            if len(non_lighthouse_instances) == 0:
                # No non-LH instance running : We make sure that
                #   the next one will be a lighthouse kind
                lighthouse_need = len(lh_ids)
            else:
                # When some non-LH instances are running, we favor to launch non-LH instances
                lighthouse_need = 0

        # Place the number of lighthouse instances needed in high priority
        instances.extend(list(filter(lambda i: i["InstanceId"] in lh_ids, candidates))[:lighthouse_need])

        if not lighthouse_only:
            for c in cfg:
                instances.extend(list(filter(lambda i: i["InstanceType"] == c["_"] and i["InstanceId"] not in lh_ids and self._match_spot(i, c), candidates)))

            # All instances with unknown instance types are low priority
            instance_types = [ c["_"] for c in cfg ]
            instances.extend(list(filter(lambda i: i["InstanceType"] not in instance_types and i["InstanceId"] not in lh_ids, candidates)))

            if not lighthouse_disabled:
                # We consider to scaleout with Lighthouse instances only if there is no running non-lighthouse instance
                #   or if all non-lighthouse instances are already started
                if (len(non_lighthouse_instances) == 0 or 
                    (self.desired_instance_count() != -1 and self.are_all_non_lh_instances_started())
                   ):
                    instances.extend(list(filter(lambda i: i["InstanceId"] in lh_ids, candidates))[lighthouse_need:])

        return instances

    def scaledown_sort_instances(self, candidates, expected_count, caller):
        cfg             = Cfg.get_list_of_dict("ec2.schedule.verticalscale.instance_type_distribution")
        instance_types  = [ i["_"] for i in cfg ]
        cfg_r           = cfg.copy()
        cfg_r.reverse()

        instances = []
        # Put instance with incorrect instance types first to make them drained first
        instances.extend(list(filter(lambda i: i["InstanceType"] not in instance_types, candidates)))


        # Pickup lighthouse instances first if enough other instances are up
        lh_ids                     = self.get_lighthouse_instance_ids(candidates)
        useable_instances          = self.get_useable_instances()
        running_lh_ids             = self.get_lighthouse_instance_ids(useable_instances)
        non_lighthouse_instances   = list(filter(lambda i: i["InstanceId"] not in lh_ids, candidates))
        lighthouse_instances       = list(filter(lambda i: i["InstanceId"] in lh_ids, candidates))
        amount_of_lh, amount_of_non_lh = self.shelve_instance_dispatch(expected_count)
        bouncing_instances         = self.ec2.get_instances(
                instances=self.get_useable_instances(exclude_bounced_instances = False), 
                State="pending,running", ScalingState="bounced")

        lighthouse_need            = -min(amount_of_lh - len(running_lh_ids), 0)
        if len(bouncing_instances) and len(non_lighthouse_instances): 
            lighthouse_need = 0 # We favor bouncing of non-LH instances first
        if self.are_lighthouse_instance_disabled(): lighthouse_need = len(lh_ids)

        # In some cases, we want to stop lighthouse instances first
        instances.extend(lighthouse_instances[:lighthouse_need])

        # We stop instance in reverse order of the distribution instance type list
        for c in cfg_r:
            instances.extend(list(filter(lambda i: i["InstanceType"] == c["_"] and self._match_spot(i, c, spot_implicit=False), non_lighthouse_instances)))

        # By default, all lighthouse instances are low priority to stop (lighthouse instances are only stopped by
        #   'shelve_extra_lighthouse_instances' process and not the 'scalin' one
        instances.extend(lighthouse_instances[lighthouse_need:])

        return instances

    @xray_recorder.capture()
    def shelve_extra_lighthouse_instances(self):
        if self.scaling_state_changed:
            return

        # If the user is forcing the fleet to run at max capacity, disable all fancy algorithms
        if Cfg.get("ec2.schedule.desired_instance_count") == "100%":
            return

        now = self.context["now"]

        min_instance_count                       = self.get_min_instance_count()
        all_instances                            = self.instances_wo_excluded                  # ec2.get_instances(ScalingState="-excluded")
        all_useable_plus_special_state_instances = self.useable_instances_wo_excluded_draining # get_useable_instances(ScalingState="-draining,excluded")
        serving_instances                        = self.serving_instances                      # get_useable_instances(exclude_initializing_instances=True)
        lh_ids                                   = self.lighthouse_instances_wo_excluded_ids       # get_lighthouse_instances(all_instances)
        running_lh_ids                           = self.get_lighthouse_instance_ids(all_useable_plus_special_state_instances)
        non_lighthouse_instances                 = self.serving_non_lighthouse_instance_ids               # list(filter(lambda i: i["InstanceId"] not in running_lh_ids, serving_instances))
        non_lighthouse_instances_initializing    = self.serving_non_lighthouse_instance_ids_initializing  # list(filter(lambda i: i["InstanceId"] not in running_lh_ids, self.get_initial_instances()))
        serving_non_lighthouse_instance_count    = len(non_lighthouse_instances) - len(non_lighthouse_instances_initializing)
        useable_instance_count                   = self.get_useable_instance_count()
        desired_instance_count                   = self.desired_instance_count()

        lighthouse_instance_excess = 0

        initializing_lh_instances_ids = self.get_lighthouse_instance_ids(self.initializing_instances) #lh_instances_idsget_initial_instances())
        if not self.are_lighthouse_instance_disabled():
            if len(initializing_lh_instances_ids):
                log.debug("Some LightHouse instances (%s) are still initializing... Postpone shelve processing..." % 
                        initializing_lh_instances_ids)
                return
            if serving_non_lighthouse_instance_count < min_instance_count and len(non_lighthouse_instances_initializing):
                log.debug("Not enough serving non-LH instances while some are initializing (%s)... Postpone shelve processing..." % 
                        non_lighthouse_instances_initializing)
                return

        max_lh_instances = len(lh_ids)
        expected_count   = desired_instance_count if desired_instance_count != -1 else useable_instance_count
        expected_count   = max(expected_count, min_instance_count)
        amount_of_lh, amount_of_non_lh = self.shelve_instance_dispatch(expected_count)

        # Take into account unhealthy/unavailable LH instances
        instances_with_issues_ids = self.instances_with_issues  # get_instances_with_issues()
        instances_with_issues     = [ i for i in self.instances_wo_excluded if i["InstanceId"] in instances_with_issues_ids] # self.ec2.get_instances(ScalingState="-excluded")
        lh_instances_with_issues  = self.get_lighthouse_instance_ids(instances_with_issues) 
        lh_instances_to_exclude   = lh_instances_with_issues.copy()
        lh_draining_instances     = self.get_lighthouse_instance_ids(
                self.ec2.get_instances(State="pending,running", ScalingState="draining,error"))
        [lh_instances_to_exclude.append(i) for i in lh_draining_instances if i not in lh_instances_to_exclude]
        # Exclude also LH Spot instances if marked for interrruption
        [lh_instances_to_exclude.append(i) for i in self.spot_excluded_instance_ids if i in lh_ids and i not in lh_instances_to_exclude]

        amount_of_lh     = min(max_lh_instances - len(lh_instances_to_exclude), amount_of_lh)

        # Compute the number of LH instances to finally add
        delta_lh         = 0
        running_lh_count = len(running_lh_ids)
        if self.are_lighthouse_instance_disabled():
            delta_lh = -running_lh_count
        elif desired_instance_count == -1: 
            # Do not stop LH instances when approaching (or leaving) min_instance_count amount of non-LH instances.
            #  => desired_instance_count == -1 so the autoscaler is active and will get rid of unnecessary instances
            if serving_non_lighthouse_instance_count > min_instance_count:
                delta_lh = -min(max(serving_non_lighthouse_instance_count - running_lh_count, 0), running_lh_count)
            else:
                # We are close to the condition where LH instances need to be started again
                #   Note: We start LH instance one at a time to avoid jerky behavior of the scalein algorithm
                delta_lh  = min(max(amount_of_lh - running_lh_count, 0), 1)
        else:
            delta_lh  = amount_of_lh - running_lh_count 

        extra_instance_count = self.get_useable_instance_count() - min_instance_count
        if extra_instance_count + delta_lh < 0:
            # We do not have enough spare instances available to reduce the fleet size without
            #   falling under min_instance_count.
            #   As consequence, we prefer to launch new fresh instances to have the opportunity
            #   later to discard the ones that should go.
            delta_lh = abs(delta_lh)

        if delta_lh != 0:
            if desired_instance_count != -1:
                if self.get_useable_instance_count(exclude_initializing_instances=True) < desired_instance_count: 
                    delta_lh = abs(delta_lh)
            else:
                if delta_lh < 0: 
                    if len(running_lh_ids) >= serving_non_lighthouse_instance_count: 
                        # Let the scalein algorithm to get rid of LH instances when needed except in the case where there are 
                        #   enough non-LH instances to fully replace them
                        delta_lh = 0
                    else:
                        # We start new instances to replace the LH ones that we will stop soon
                        pre_lh_stop_date = self.ec2.get_state_date("ec2.schedule.shelve.pre_lh_stop_date")
                        if pre_lh_stop_date is None:
                            self.set_state("ec2.schedule.shelve.pre_lh_stop_date", now)
                            delta_lh = running_lh_count
                        elif (now - pre_lh_stop_date).total_seconds() < Cfg.get_duration_secs("ec2.schedule.verticalscale.lighthouse_replacement_graceperiod"):
                            delta_lh = 0
                        else:
                            self.set_state("ec2.schedule.shelve.pre_lh_stop_date", "")
            
            if delta_lh != 0:
                self.instance_action(useable_instance_count + delta_lh, "shelve", target_for_dispatch=expected_count)

    @xray_recorder.capture()
    def compute_instance_type_plan(self):
        if Cfg.get_int("ec2.schedule.verticalscale.disable_instance_type_plannning"):
            return
        cfg             = Cfg.get_list_of_dict("ec2.schedule.verticalscale.instance_type_distribution")
        fleet_instances = self.instances_wo_excluded # ec2.get_instances(ScalingState="-excluded")
        fleet_size      = len(fleet_instances)

        schedule_size        = fleet_size
        cfg                  = cfg[:schedule_size] # Can't have more instance type definitions than fleet size
        nb_of_instance_types = len(cfg) 

        expected_distribution      = defaultdict(int)
        instance_types       = []
        for i in range(0, nb_of_instance_types):
            instance_type                   = cfg[i]["_"]
            count                           = schedule_size / nb_of_instance_types
            if "count" in cfg[i]:
                try:
                    count                 = int(cfg[i]["count"])
                    schedule_size        -= count
                    nb_of_instance_types -= 1
                except Exception as e:
                    log.exception("Failed to convert count=%s into integer! %s" % (cfg[i]["count"], e))
            expected_distribution[instance_type] += count 
            if instance_type not in instance_types: instance_types.append(instance_type)

        # Round number of instances
        allocated_instances  = 0
        for typ in instance_types:
            count                = round(expected_distribution[typ])
            expected_distribution[typ] = count
            allocated_instances += count
        if len(instance_types) and fleet_size > allocated_instances: 
            expected_distribution[instance_types[-1:][0]] += fleet_size - allocated_instances

        # Compute what is missing
        for inst in fleet_instances:
            instance_type = inst["InstanceType"]
            if instance_type in expected_distribution.keys():
                expected_distribution[instance_type] -= 1

        stopped_instances = self.stopped_instances_wo_excluded # ec2.get_instances(State="stopped", ScalingState="-excluded")
        i = 0
        max_modified_per_batch = Cfg.get_int("ec2.schedule.verticalscale.max_instance_type_modified_per_batch")
        for inst in stopped_instances:
            typ = inst["InstanceType"]

            # Test if this instance has already the right instance type
            if typ in instance_types and expected_distribution[typ] >= 0: 
                # It looks so but check if a more prioritary instance type needs to be fulfilled first
                index = instance_types.index(typ)
                if len(list(filter(lambda t: expected_distribution[t] > 0, instance_types[:index]))) == 0:
                    continue

            if self.ec2.is_spot_instance(inst): continue # Can't change instance type for Spot instance

            for t in instance_types:
                if expected_distribution[t] <= 0: continue # Too many instances of this type

                # Need to update the instance type for this instance
                instance_id = inst["InstanceId"]
                response    = None
                try:
                    response = self.context["ec2.client"].modify_instance_attribute(
                            InstanceId=instance_id, InstanceType={
                                  'Value': t,
                            })
                except Exception as e:
                    log.exception("Failed to modify InstanceType for instance '%s' : %s" % (instance_id, e))

                if response is None or response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                    log.error("Failed to change instance type for instance %s! : %s" % (instance_id, response))
                else:
                    self.scaling_state_changed = True
                    max_modified_per_batch -= 1
                    expected_distribution[t] -= 1
                    break

            if max_modified_per_batch < 0: 
                break





    ###############################################
    #### CORE SCALEIN/OUT ALGORITHM ###############
    ###############################################

    def get_scale_start_date(self, direction):
        return self.ec2.get_state_date("ec2.schedule.%s.start_date" % direction)

    def is_scale_transition_too_early(self, to_direction):
        now = self.context["now"]
        opposite = "scaleout" if to_direction == "scalein" else "scaleout"
        last_scale_action_date = self.ec2.get_state_date("ec2.schedule.%s.last_action_date" % opposite)
        if last_scale_action_date is None:
            return 0

        time_to_wait =  timedelta(seconds=Cfg.get_duration_secs("ec2.schedule.to_%s_state.cooldown_delay" % to_direction)) - (now - last_scale_action_date)
        seconds = time_to_wait.total_seconds()

        return seconds if seconds > 0 else 0

    @xray_recorder.capture()
    def get_guilties_sum_points(self, assessment, default_points):
        now                     = self.context["now"]
        useable_instances_count = self.get_useable_instance_count(exclude_initializing_instances=True)
        alarm_with_metrics      = self.cloudwatch.get_alarm_names_with_metrics()
        alarm_in_ALARM          = [ a["AlarmName"] for a in assessment["upscale"]["guilties"] ]

        all_alarm_names         = alarm_with_metrics.copy()
        all_alarm_names.extend(list(filter(lambda a: a not in alarm_with_metrics, alarm_in_ALARM)))

        # Target for weighted unknown divider:
        #   When a divider is not specified, the algorithm will divide the individual scores by the number of
        #   instances. Without a margin, the compute overall score could reach 1.0 at the very moment where
        #   almost all alarms will trig giving their full score and making the autoscaler scales aggresssively. 
        #   In order to avoid such erratic behavior, we implement a margin that will ensure the scaling score
        #   will reach 1.0 (so scaling) before invidual alarms are close to trig.
        unknown_divider_target = min(1.0, max(0.1, Cfg.get_float("ec2.schedule.horizontalscale.unknown_divider_target")))

        oldest_metric_secs   = 0
        for a in alarm_with_metrics:
            metric = self.cloudwatch.get_metric_by_id(a)
            if metric is not None:
                metric_date = misc.str2utc(metric["_SamplingTime"])
                oldest_metric_secs = max(oldest_metric_secs, (now - metric_date).total_seconds())
        oldest_metric_secs   = max(0.001, oldest_metric_secs) # Avoid DIV#0 later in the algorithm


        all_points       = defaultdict(dict)
        sum_of_deltas    = 1 
        sum_of_unkwnown_divider_delta_time = 0
        scores           = []
        no_metric_alarms = []
        for alarm_name in all_alarm_names:
            alarm_def           = self.cloudwatch.get_alarm_configuration_by_name(alarm_name)
            if alarm_def is None: 
                continue

            instance_id  = alarm_def["InstanceId"] if "InstanceId" in alarm_def else None
            meta         = alarm_def["AlarmDefinition"]["Metadata"]
            alarm_group  = alarm_def["AlarmDefinition"]["Metadata"]["AlarmGroup"] if "AlarmGroup" in alarm_def["AlarmDefinition"]["Metadata"] else None

            k = "alarmname:%s" % alarm_name if alarm_group is None else "alarmgroup:%s" % alarm_group
            if instance_id is not None: k = "instance:%s" % instance_id

            all_points[k][alarm_name] = 0
            alarm_points              = int(default_points)
            if "Points" in meta:
                try:
                    alarm_points = int(meta["Points"])
                except:
                    log.exception("[WARNING] Failed to process 'Points' metadata for alarm %s! (%s)" % (alarm_name, meta["Points"]))

            metric_data        = self.cloudwatch.get_alarm_data_by_name(alarm_name)
            if alarm_name in alarm_in_ALARM or (metric_data is not None and metric_data["StateValue"] == "ALARM"):
                # Alarm that are in ALARM state are directly earning their points
                all_points[k][alarm_name] = int(alarm_points)

            if "MetricDetails" not in metric_data or len(metric_data["MetricDetails"]["Values"]) == 0:
                no_metric_alarms.append(alarm_name)
                continue

            latest_metric_value= float(metric_data["MetricDetails"]["Values"][0])
            alarm_threshold    = float(metric_data["Threshold"])

            reverse_alarm      = "Less" in metric_data["ComparisonOperator"]
            baseline_threshold = alarm_threshold * 3.0/1.0 if reverse_alarm else alarm_threshold * 1.0/3.0 # By default
            try:
                if "BaselineThreshold" in meta and meta["BaselineThreshold"] != "": 
                    baseline_threshold = float(meta["BaselineThreshold"])
            except Exception as e:
                log.error("Failed to process 'BaselineThreshold' in %s/%s : %s" % 
                    (alarm_def["Key"], alarm_def["Definition"]))

            gap       = abs(alarm_threshold - baseline_threshold)
            if gap == 0: continue
            gap_ratio = (latest_metric_value - baseline_threshold) / gap 

            # Extrapolation algorithm
            #   Youngest metric data got 80% weight ; oldest get 20% 
            pcent = 0.20 + (0.6 * (oldest_metric_secs - (now - misc.str2utc(metric_data["MetricDetails"]["_SamplingTime"])).total_seconds()) / oldest_metric_secs)
            s = {
                "AlarmName": alarm_name,
                "ResourceKey": k,
                "GapRatio": gap_ratio,
                "DividerWeight": pcent
                }
            try:
                if "Divider" in meta:
                    s["Divider"] = float(meta["Divider"])
            except: 
                log.warning("Failed to convert Divider '%s' as float for alarm '%s'!" % (meta["Divider"], alarm_name))
            if not "Divider" in s: 
                sum_of_unkwnown_divider_delta_time += s["DividerWeight"]
            scores.append(s)

        log.log(log.NOTICE, "No metric available yet for '%s'..." % no_metric_alarms)

        for s in scores:
            # Determine the divider to use (default is the amount of useable instances)
            if "Divider" in s:
                weight    = 1 / s["Divider"]
                gap_ratio = s["GapRatio"] 
            else:

                weight    = s["DividerWeight"] / float(sum_of_unkwnown_divider_delta_time) 
                gap_ratio = s["GapRatio"] / unknown_divider_target

            score_points = alarm_points * gap_ratio * weight

            alarm_name   = s["AlarmName"]
            resource_key = s["ResourceKey"]
            all_points[resource_key][alarm_name] = max(all_points[resource_key][alarm_name], int(score_points))

        # Take only the biggest score point per instance
        points = 0
        for k in all_points.keys():
            ps = [ all_points[k][p] for p in all_points[k] ]
            ps = sorted(ps, reverse=True)
            if len(ps): 
                log.info("Scores for '%s' : %s" % (k, all_points[k]))
                points += ps[0]
        return points


    @xray_recorder.capture()
    def scale_in_out(self):
        """
        This is the function that takes decisions about starting or stopping instances based on Alarms
        """

        now = self.context["now"]

        # Main decision loop 
        assessment = {
                "upscale" : {
                        "guilties" : []
                    }
            }
        items = self.ec2_alarmstate_table.get_items()
        for item in items:
            instance_id = item["InstanceId"]
            alarm_name  = item["AlarmName"]
            p = "[%s/%s]" % (instance_id, alarm_name)

            ALARM_LastAlarmTimeStamp = misc.str2utc(item["ALARM_LastAlarmTimeStamp"]) if "ALARM_LastAlarmTimeStamp" in item else None
            OK_LastAlarmTimeStamp    = misc.str2utc(item["OK_LastAlarmTimeStamp"]) if "OK_LastAlarmTimeStamp" in item else None

            if ALARM_LastAlarmTimeStamp is None:
                continue

            if OK_LastAlarmTimeStamp is not None and OK_LastAlarmTimeStamp >= ALARM_LastAlarmTimeStamp:
                continue

            # Here, we know that there is a valid Alarm 
            if "ALARM_Event" not in item:
                log.debug("Invalid record (missing event field)")
                return

            event = item["ALARM_Event"]
            if OK_LastAlarmTimeStamp is None or OK_LastAlarmTimeStamp < ALARM_LastAlarmTimeStamp:
                # This alarm has an inconsistent OK state for a long time so that is suspicous (ex: We
                #      may have missed one OK SNS message.
                # In order to mitigate this kind of event, we read the metric directly
                response = self.context["cloudwatch.client"].describe_alarms(
                            AlarmNames=[item["AlarmName"]]
                )
                log.debug("Suspicious long running alarm. Getting alarm state '%s' directly : %s" % (item["AlarmName"], response))
                if "MetricAlarms" not in response or len(response["MetricAlarms"]) == 0:
                    # Ignore this alarm as we failed to get a valid status (Alarm was deleted?)
                    continue

                if response["MetricAlarms"][0]["StateValue"] != "ALARM":
                    log.debug("Detected an Alarm item '%s' that is not backed by a CloudWatch ALARM state. Ignoring." % alarm_name)
                    continue
            # This item looks as an alarm condition
            assessment["upscale"]["guilties"].append(item)

        # Calculate decision
        self.take_scale_decision(items, assessment)

    @xray_recorder.capture()
    def get_scale_instance_count(self, direction, boost_rate, text):
        now = self.context["now"]

        instance_upfront_count = 0
        last_scale_start_date = self.get_scale_start_date(direction)
        new_scale_sequence    = False
        if last_scale_start_date is None:
            # Remember when we started to scale up
            last_scale_start_date = now
            last_event_date       = last_scale_start_date
            new_scale_sequence    = True
            # Do we have to (over)react because of new sequence?
            instance_upfront_count = Cfg.get_int("ec2.schedule.%s.instance_upfront_count" % direction)
        else:
            last_event_date       = self.ec2.get_state_date("ec2.schedule.%s.last_action_date" % direction, default=last_scale_start_date)
            if last_event_date < last_scale_start_date: last_event_date = last_scale_start_date

        seconds_since_scale_start        = now - last_scale_start_date
        seconds_since_latest_scale_event = now - last_event_date

        period = Cfg.get_duration_secs("ec2.schedule.%s.period" % direction) 
        rate   = Cfg.get_int("ec2.schedule.%s.rate" % direction)

        ratio              = float(rate) / (float(period) / boost_rate)
        raw_instance_count = ratio * seconds_since_latest_scale_event.total_seconds() 

        delta_count = int(raw_instance_count) + instance_upfront_count 
        # Ensure that we never fall under 'min_instance_count' running instances
        if direction == "scalein":
            min_instance_count        = self.get_min_instance_count()
            running_instances_count   = self.get_useable_instance_count(exclude_problematic_instances=False)
            max_instance_suppressed   = running_instances_count - min_instance_count 
            if max_instance_suppressed <= 0: 
                # Algorithm can't go below 'min_instance_count' so we reset it
                self.set_state("ec2.schedule.%s.start_date" % direction, "")
                text.append("Scale Down can't reduce fleet size below 'min_instance_count")
                return 0

            delta_count = min(delta_count, max_instance_suppressed)

        if new_scale_sequence:
            self.set_state("ec2.schedule.%s.start_date" % direction, str(last_scale_start_date))

        text.append("\n".join(["[INFO] Scaling data for direction '%s' :" % direction,
                "  Now: %s" % now,
                "  [INFO] Scale start date: %s" % str(last_scale_start_date),
                "  [INFO] Latest scale event date: %s" % str(last_event_date),
                "  [INFO] Seconds since scale start: %d" % seconds_since_scale_start.total_seconds(),
                "  [INFO] Seconds since latest scale event date: %d" % seconds_since_latest_scale_event.total_seconds(),
                "  [INFO] Rate: %d instance(s) per period" % rate,
                "  [INFO] Nominal Period: %d seconds" % period,
                "  [INFO] Boost rate: %f" % boost_rate,
                "  [INFO] Boosted effective period: %d seconds" % (period / boost_rate),
                "  [INFO] Effective rate over %ds period: %.1f instance per period (%.1f instance(s) per minute)" % (period, ratio * period, ratio * 60),
                "  Computed raw instance count: %.2f" % raw_instance_count
                ]))

        return delta_count

    @xray_recorder.capture()
    def take_scale_decision(self, items, assessment):
        now = self.context["now"]

        # Step 1) Calculate the number of instances to start or stop
        
        base_points                   = Cfg.get_int("ec2.schedule.base_points")
        self.alarm_points             = self.get_guilties_sum_points(assessment, base_points)
        self.raw_instance_scale_score = float(self.alarm_points) / float(base_points)

        # To avoid jerky scale score, we integrate it over a period of time
        # Integrate Raw Instance scale score
        integration_period        = Cfg.get_duration_secs("ec2.schedule.horizontalscale.raw_integration_period")
        self.ec2.set_integrated_float_state("ec2.schedule.scaleout.raw_instance_scale_score", self.raw_instance_scale_score, 
                integration_period, self.state_ttl)
        self.integrated_raw_instance_scale_score = self.ec2.get_integrated_float_state("ec2.schedule.scaleout.raw_instance_scale_score",
                integration_period, default=self.raw_instance_scale_score, favor_max_value=False)
        # Integrate synthetic Instance scale score
        integration_period        = Cfg.get_duration_secs("ec2.schedule.horizontalscale.integration_period")
        self.ec2.set_integrated_float_state("ec2.schedule.scaleout.instance_scale_score", self.raw_instance_scale_score, 
                integration_period, self.state_ttl)
        self.instance_scale_score = self.ec2.get_integrated_float_state("ec2.schedule.scaleout.instance_scale_score",
                integration_period, default=self.raw_instance_scale_score)

        log.info("Scale score: (Raw=%f/IntegratedRaw=%f/Integrated=%f)" % 
                (self.raw_instance_scale_score, self.integrated_raw_instance_scale_score, self.instance_scale_score))

        if self.desired_instance_count() != -1:
            log.info("Autoscaler disabled due to 'ec2.schedule.desired_instance_count' set to a value different than -1!")
            return

        scale_up_disabled = Cfg.get_int("ec2.schedule.scaleout.disable") != 0
        if scale_up_disabled: log.log(log.NOTICE, "ScaleOut scheduler disabled!")

        if self.scaling_state_changed:
            return

        if not scale_up_disabled and self.instance_scale_score >= 1.0:
            self.take_scale_decision_scaleout()
            return

        # Remember that we are not in an scaleout condition here
        self.set_state("ec2.schedule.scaleout.start_date", "")
        self.set_state("ec2.schedule.scaleout.last_action_date", "")

        # Step 2) Check if we are allowed to downscale now
        scale_down_disabled = Cfg.get_int("ec2.schedule.scalein.disable") != 0
        if scale_down_disabled: log.log(log.NOTICE, "ScaleIn scheduler disabled!")

        if not scale_down_disabled and self.instance_scale_score < Cfg.get_float("ec2.schedule.scalein.threshold_ratio"):
            self.take_scale_decision_scalein()
            return

        # Remember that we are not in an scalein condition here
        self.set_state("ec2.schedule.scalein.start_date", "")
        self.set_state("ec2.schedule.scalein.last_action_date", "")

    @xray_recorder.capture()
    def take_scale_decision_scaleout(self):
        now = self.context["now"]
        time_to_wait = self.is_scale_transition_too_early("scaleout")
        if time_to_wait > 0:
            log.log(log.NOTICE, "Transition period from scalein to scaleout (%d seconds still to go...)" % time_to_wait)
            return

        text = []
        instance_to_start = self.get_scale_instance_count("scaleout", self.instance_scale_score, text)
        if instance_to_start == 0: 
            self.would_like_to_scaleout = True
            return
        log.debug(text[0])
        useable_instances_count = self.get_useable_instance_count()
        desired_instance_count = useable_instances_count + instance_to_start

        log.log(log.NOTICE, "Need to start up to '%d' more instances (Total expected=%d)" % (instance_to_start, desired_instance_count))
        self.instance_action(desired_instance_count, "scaleout")
        self.set_state("ec2.schedule.scaleout.last_action_date", now)

    @xray_recorder.capture()
    def take_scale_decision_scalein(self):
        now = self.context["now"]
        # Reset the scalein algorithm while targets are in 'initial' state
        initial_target_instance_ids = self.get_initial_instances_ids()
        if len(initial_target_instance_ids):
            log.log(log.NOTICE, "Instances %s are still initializing... Delaying scalein assessment..." % initial_target_instance_ids)
            self.set_state("ec2.schedule.scalein.start_date", "",
                    TTL=self.state_ttl)
            return

        time_to_wait = self.is_scale_transition_too_early("scalein")
        if time_to_wait > 0:
            log.log(log.NOTICE, "Transition period from scaleout to scalein (%d seconds still to go...)" % time_to_wait)
            return

        text = []
        scalein_threshold = Cfg.get_float("ec2.schedule.scalein.threshold_ratio")
        # Scale in rate depends on the distance of instance scale score and the scalein threshold 
        scalein_rate      = 1.0 - (self.instance_scale_score / scalein_threshold)
        instance_to_stop  = self.get_scale_instance_count("scalein", scalein_rate, text)
        if instance_to_stop == 0:
            self.would_like_to_scalein = True
            return
        log.debug(text[0])
        active_instance_count  = self.get_useable_instance_count(exclude_problematic_instances=False)
        desired_instance_count = active_instance_count - instance_to_stop
        if desired_instance_count < 0: desired_instance_count = 0

        log.log(log.NOTICE, "Need to stop up to '%d' more instances (Total expected=%d)" % (instance_to_stop, desired_instance_count))
        self.instance_action(desired_instance_count, "scalein")
        self.set_state("ec2.schedule.scalein.last_action_date", now)

    ###############################################
    #### SPOT MANAGEMENT ##########################
    ###############################################

    def compute_spot_exclusion_lists(self):
        # Collect all Spot instances events
        self.spot_rebalance_recommanded = self.ec2.filter_spot_instances(self.instances_wo_excluded, EventType="+rebalance_recommended")
        self.spot_rebalance_recommanded_ids = [ i["InstanceId"] for i in self.spot_rebalance_recommanded ]
        self.spot_interrupted           = self.ec2.filter_spot_instances(self.instances_wo_excluded, EventType="+interrupted")
        self.spot_interrupted_ids       = [ i["InstanceId"] for i in self.spot_interrupted ]

        for event_type in [self.spot_rebalance_recommanded, self.spot_interrupted]:
            for i in event_type:
                instance_type  = i["InstanceType"]
                instance_az    = i["Placement"]["AvailabilityZone"]
                if instance_type not in self.excluded_spot_instance_types:
                    self.excluded_spot_instance_types.append({
                        "AvailabilityZone" : instance_az,
                        "InstanceType"     : instance_type
                        })

        if len(self.excluded_spot_instance_types):
            log.warning("Some instance types (%s) are temporarily blacklisted for Spot use as marked as interrupted or close to interruption!" %
                    self.excluded_spot_instance_types)

        self.instances_wo_excluded_error_spotexcluded = self.ec2.filter_spot_instances(self.instances_wo_excluded_error, 
                filter_out_instance_types=self.excluded_spot_instance_types)
        # Gather all instance ids of interrupted Spot instances
        for i in self.ec2.filter_spot_instances(self.instances_wo_excluded, filter_in_instance_types=self.excluded_spot_instance_types, match_only_spot=True):
            self.spot_excluded_instance_ids.append(i["InstanceId"])

    def manage_spot_events(self):
        if len(self.spot_rebalance_recommanded):
            log.log(log.NOTICE, "EC2 Spot instances with 'rebalance_recommended' status: %s" % self.spot_rebalance_recommanded)
        if len(self.spot_interrupted):
            log.log(log.NOTICE, "EC2 Spot instances with 'interrupted' status: %s" % self.spot_interrupted)

        for i in self.spot_interrupted:
            instance_id    = i["InstanceId"]
            if i["State"]["Name"] == "running":
                self.ec2.set_scaling_state(instance_id, "draining")
                log.info("Set 'draining' state for Spot interrupted instance '%s'." % instance_id)


