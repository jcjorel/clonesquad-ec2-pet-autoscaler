""" ec2_schedule.py

License: MIT

This module is responsible to take all scheduling decisions of EC2 instances under management.

As a top level module, it as dependencies with almost all lower level modules (state.py, misc.py, targetgroup.py, ec2.py).

The modules manages:
    * Autoscaling of Main fleet,
        - ScalinIn/ScaleOut algorithms,
        - Vertical scaling algorithm.
    * Manual scaling of Main fleet and subfleet,
    * Instance bouncing,
    * Business logic for Spot management ('ec2.py' is responsible for the machinery to intercept and store the EC2 Spot SQS messages),
    * Publication of EC2 scaling metrics (from Main and Subfleets)
    * Generation of scaling events ("run_instances", "stop_instances"...)
    * Creation and Update of CloudWatch dashboards (Main and Subfleets)

As all other major modules, the get_prerequisites() method will pre-compute all data needed for all the algorithms. The overall logic
is that all code outside the get_prerequisites() must work only with data gathered and synthesized in get_prerequisites(). This constraint
ensure easier debugging and more predectible behaviors of various algorithms.

__init__():
    - Registers configuration and CloudWatch attached to the local namespace ("ec2.schedule" here).

get_prerequisites():
    - Scaling algorithms are making an intensive use of instance list sorted and filtered in various manner. To avoid a constant recalculation,
        most used list are pre-computed in this function.

schedule_instances():
    - Module entrypoint that will dispatch sequentially works to the remaining module code.

"""
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
import ssm
import config as Cfg
import debug as Dbg
from subfleet import get_subfleet_key
from subfleet import get_subfleet_key_abs_or_percent

from aws_xray_sdk.core import xray_recorder

import cslog
log = cslog.logger(__name__)

class EC2_Schedule:
    @xray_recorder.capture(name="EC2_Schedule.__init__")
    def __init__(self, context, ec2, targetgroup, cloudwatch):
        """ Initialize the module-wide variables and register configuration keys and associated documentation.
        
        NOTE: This function must be calleable from the cs-format-documentation tool so dependencies must be kept light.
        """
        self.context                  = context
        self.ec2                      = ec2
        self.o_state                  = self.context["o_state"]
        self.targetgroup              = targetgroup
        self.cloudwatch               = cloudwatch
        self.ssm                      = self.context["o_ssm"]
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
system issues.

                 """
                 },
                 "ec2.schedule.desired_instance_count,Stable" : {
                     "DefaultValue" : -1,
                     "Format"       : "IntegerOrPercentage", 
                     "Description"  : """If set to -1, the autoscaler controls freely the number of running instances. Set to a value different than -1,
the autoscaler is disabled and this value defines the number of serving (=running & healthy) instances to maintain at all time.
The [`ec2.schedule.min_instance_count`](#ec2schedulemin_instance_count) is still authoritative and the 
[`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count) parameter cannot bring
the serving fleet size below this hard lower limit. 

A typical usage for this key is to set it to `100%` to temporarily force all the instances to run at the same time to perform mutable maintenance
(System and/or SW patching).

> Tip: Setting this key to the special `100%` value has also the side effect to disable all instance health check management and so ensure the whole fleet running 
at its maximum size in a stable manner (i.e. even if there are impaired/unhealthy instances in the fleet, they won't be restarted automatically).

> **Important tip related to LightHouse instances**:  In a maintenance use-case, users may require to have all instances **including LightHouse ones** up and running; setting both [`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count) and [`ec2.schedule.min_instance_count`](#ec2schedulemin_instance_count) to the string value `100%` will start ALL instances.
                     """
                 },
                 "ec2.schedule.max_instance_start_at_a_time" : 10,
                 "ec2.schedule.max_instance_stop_at_a_time" : 5,
                 "ec2.schedule.state_ttl" : "hours=2",
                 "ec2.schedule.base_points" : 1000,
                 "ec2.schedule.assume_cpu_exhausted_burstable_instances_as_unuseable": "0",
                 "ec2.schedule.disable,Stable": {
                    "DefaultValue" : 0,
                    "Format"        : "Bool",
                    "Description"   : """Disable all scale or automations algorithm in the Main fleet. 

Setting this parameter to '1' disables all scaling and automation algorithms in the Main fleet. While set, all Main fleet instances can be freely started 
and stopped by the users without CloneSquad trying to manage them. 

Note: It is semantically similar to the value `undefined` in subfleet configuration key [`subfleet.{SubfleetName}.state`](#subfleetsubfleetnamestate).
                    """
                 },
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
                     "Description"  : """Period of scaling assessment. 

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

    Ex: t3.medium,lighthouse;c5.large,spot;c5.large;c5.xlarge

Please consider reading [detailed decumentation about vertical scaling](SCALING.md#vertical-scaling) to ensure proper use.
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
                 "ec2.schedule.bounce.instances_with_issue_grace_period": "minutes=10",
                 "ec2.schedule.draining.instance_cooldown,Stable": {
                         "DefaultValue": "minutes=2",
                         "Format": "Duration",
                         "Description": """Minimum time to spend in the 'draining' state.

If SSM support is enabled with [`ssm.feature.events.ec2.instance_ready_for_shutdown`](#ssmfeatureeventsec2instance_ready_for_shutdown), a script located in the drained instance is executed to ensure that the instance is ready for shutdown even after the specified duration is exhausted. If this script returns non-zero code, the shutdown is postponed for a maximum duration defined in [`ssm.feature.events.ec2.instance_ready_for_shutdown.max_shutdown_delay`](#ssmfeatureeventsec2instance_ready_for_shutdownmax_shutdown_delay).
                """
                },
                 "ec2.schedule.start.warmup_delay,Stable": {
                    "DefaultValue": "minutes=3",
                    "Format": "Duration",
                    "Description": """Minimum delay for node readiness.

After an instance start, CloneSquad will consider it in 'initializing' state for the specified minimum amount of time.

When at least one instance is in 'initializing' state in a fleet, no other instance can be placed in `draining` state meanwhile:
This delay is meant to let new instances to succeed their initialization.

If an application takes a long time to be ready, it can be useful to increase this value.
                 """
                 },
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
                         "DefaultValue": 0,
                         "Format"      : "Bool",
                         "Description" : """Enable a weekly wakeup of burstable instances ["t3","t4"]

This flag enables an automatic wakeup of stopped instances before the one-week limit that would mean accrued CPU Credit loss.
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
                         "DefaultValue": "minutes=0",
                         "Format"      : "Duration",
                         "Description" : """Minimum duration a Spot instance needs to spend in 'stopped' state before to allow a start.

A very recently stopped persistent Spot instance may not be restartable immediatly for AWS technical reasons. This 
parameter is kept for backward compatibility. Since version 0.13, CloneSquad is able to manage automatically Spot instances
that need a technical grace period so there should no need to set a value different of 0 for this parameter.
                         """
                 },
                 "cloudwatch.subfleet.use_dashboard,Stable": {
                         "DefaultValue": "1",
                         "Format": "Bool",
                         "Description": """Enable or disabled the dashboard dedicated to Subfleets.

By default, the dashboard is enabled.

> Note: The dashboard is configured only if there is at least one Subfleet with detailed metrics.
                 """},
                 "ec2.schedule.verticalscale.disable_instance_type_plannning": 0
        })

        self.state_ttl = Cfg.get_duration_secs("ec2.schedule.state_ttl")

        # Register Metrics for this module
        self.metric_time_resolution = Cfg.get_int("ec2.schedule.metrics.time_resolution")
        if self.metric_time_resolution or misc.is_sam_local() < 60: metric_time_resolution = 1 # Switch to highest resolution

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
                { "MetricName": "Subfleet.EC2.Size",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "Subfleet.EC2.RunningInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "Subfleet.EC2.DrainingInstances",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
                { "MetricName": "SSM.MaintenanceWindow",
                  "Unit": "Count",
                  "StorageResolution": self.metric_time_resolution },
            ])

        # All state keys deeper than 'ec2.scheduler.instance.*' are collapsed in an aggregate stored compressed in DynamoDB
        #   (Aggregates are used to reduce calls to DynamoDB and so associated costs).
        self.ec2.register_state_aggregates([
            {
                "Prefix": "ec2.schedule.instance.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("ec2.schedule.state_ttl")
            }
            ])



    def get_prerequisites(self):
        """ This method loads, gathers and prepares data needed by all others methods in this module.
        """
        self.cpu_credits          = yaml.safe_load(str(misc.get_url("internal:cpu-credits.yaml"),"utf-8"))
        self.ec2_alarmstate_table = kvtable.KVTable(self.context, self.context["AlarmStateEC2Table"])
        self.ec2_alarmstate_table.reread_table()

        # Read instance control state
        self.instance_control     = self.ec2.get_instance_control_state()
        self.unstoppable_ids      = list(self.instance_control["unstoppable"].keys())
        self.unstartable_ids      = list(self.instance_control["unstartable"].keys())
        if len(self.unstoppable_ids):
            log.info("Instances %s are marked as 'unstoppable'." % self.unstoppable_ids)
        if len(self.unstartable_ids):
            log.info("Instances %s are marked as 'unstartable'." % self.unstartable_ids)

        # The scheduler part is making an extensive use of filtered/sorted lists that could become
        #   cpu and time consuming to build. We build here a library of filtered/sorted lists 
        #   available to all algorithms.
        # Library of filtered/sorted lists excluding the 'excluded' instances
        xray_recorder.begin_subsegment("prerequisites:prepare_instance_lists")
        log.debug("Computing all instance lists needed for scheduling")
        self.all_instances                                        = self.ec2.get_instances()
        self.all_main_fleet_instances                             = self.ec2.get_instances(main_fleet_only=True)
        self.pending_running_instances                            = self.ec2.get_instances(State="pending,running")
        self.instances_wo_excluded                                = self.ec2.get_instances(ScalingState="-excluded")
        self.instances_wo_excluded_error                          = self.ec2.get_instances(instances=self.instances_wo_excluded, ScalingState="-error")
        self.running_instances_wo_excluded                        = self.ec2.get_instances(instances=self.instances_wo_excluded, State="running")
        self.pending_instances_wo_draining_excluded               = self.ec2.get_instances(instances=self.instances_wo_excluded, State="pending", ScalingState="-draining")
        self.stopped_instances_wo_excluded                        = self.ec2.get_instances(instances=self.instances_wo_excluded, State="stopped")
        self.stopped_instances_wo_excluded_error                  = self.ec2.get_instances(instances=self.instances_wo_excluded, State="stopped", ScalingState="-error")
        self.stopping_instances_wo_excluded                       = self.ec2.get_instances(instances=self.instances_wo_excluded, State="stopping")
        self.pending_running_instances_draining_wo_excluded       = self.ec2.get_instances(instances=self.instances_wo_excluded, State="pending,running", ScalingState="draining")
        self.pending_running_instances_bounced_wo_excluded        = self.ec2.get_instances(instances=self.instances_wo_excluded, State="pending,running", ScalingState="bounced")
        self.pending_running_instances_wo_excluded                = self.ec2.get_instances(instances=self.instances_wo_excluded, State="pending,running")
        self.pending_running_instances_wo_excluded_draining_error = self.ec2.get_instances(instances=self.instances_wo_excluded, State="pending,running", ScalingState="-draining,error")
        # Other filtered/sorted lists
        self.stopped_instances_wo_excluded_error    = self.ec2.get_instances(State="stopped", ScalingState="-excluded,error")
        self.pending_running_instances_draining     = self.ec2.get_instances(State="pending,running", ScalingState="draining")
        self.excluded_instances                     = self.ec2.get_instances(ScalingState="excluded")
        self.error_instances                        = self.ec2.get_instances(ScalingState="error")
        self.non_burstable_instances                = self.ec2.get_non_burstable_instances()
        self.stopped_instances_bounced_draining     = self.ec2.get_instances(State="stopped", ScalingState="bounced,draining")

        # Useable and serving instances
        self.compute_spot_exclusion_lists()
        self.initializing_instances                 = self.get_initial_instances()
        self.cpu_exhausted_instances                = self.get_cpu_exhausted_instances()
        self.unhealthy_instances_in_targetgroups    = self.targetgroup.get_registered_instance_ids(state="unavail,unhealthy")
        self.subfleet_cpu_crediting_ids             = defaultdict(list)
        self.need_mainfleet_cpu_crediting_ids       = []
        self.need_cpu_crediting_instance_ids        = self.compute_cpu_crediting_instances(self.need_mainfleet_cpu_crediting_ids, 
                self.subfleet_cpu_crediting_ids)
        self.ready_for_operation_timeouted_instances = self.get_ready_for_operation_timeouted_instances()
        self.ready_for_shutdown_timeouted_instances = self.get_ready_for_shutdown_timeouted_instances()
        self.instances_with_issues                  = self.get_instances_with_issues()
        self.useable_instances                      = self.get_useable_instances()
        self.useable_instance_count                 = self.get_useable_instance_count()
        self.useable_instances_wo_excluded_draining = self.get_useable_instances(instances=self.instances_wo_excluded, ScalingState="-draining")
        self.serving_instances                      = self.get_useable_instances(exclude_initializing_instances=True)

        # LightHouse filtered/sorted lists
        self.lighthouse_instances_wo_excluded_ids     = self.get_lighthouse_instance_ids(self.instances_wo_excluded)
        self.draining_lighthouse_instances_ids        = self.get_lighthouse_instance_ids(instances=self.pending_running_instances_draining_wo_excluded)
        self.serving_lighthouse_instances_ids         = self.get_lighthouse_instance_ids(self.serving_instances)
        self.useable_lighthouse_instance_ids          = self.get_lighthouse_instance_ids(self.useable_instances)
        self.serving_non_lighthouse_instance_ids      = list(filter(lambda i: i["InstanceId"] not in self.lighthouse_instances_wo_excluded_ids, self.useable_instances_wo_excluded_draining))
        self.serving_non_lighthouse_instance_ids_initializing = list(filter(lambda i: i["InstanceId"] not in self.useable_lighthouse_instance_ids, self.get_useable_instances(initializing_only=True)))
        self.lh_stopped_instances_wo_excluded_error   = self.get_lighthouse_instance_ids(instances=self.stopped_instances_wo_excluded_error)

        # Subfleets
        self.subfleet_instances              = self.ec2.get_subfleet_instances()
        self.subfleet_instances_w_excluded   = self.ec2.get_subfleet_instances(with_excluded_instances=True)
        self.running_subfleet_instances      = self.ec2.get_instances(instances=self.subfleet_instances, State="pending,running")
        self.draining_subfleet_instances     = self.ec2.get_instances(instances=self.subfleet_instances, ScalingState="draining")
        log.debug("End of instance list computation.")
        xray_recorder.end_subsegment()


        # Garbage collect incorrect/unsync statuses (can happen when user stop the instance 
        #   directly on the AWS console)
        instances = self.stopped_instances_bounced_draining 
        for i in instances:
            instance_id = i["InstanceId"]
            log.debug("Garbage collect instance '%s' with improper 'draining' status..." % instance_id)
            self.ec2.set_scaling_state(instance_id, "")

        # Garbage collect zombie states (i.e. instances do not exist anymore but have still states in state table)
        instances = self.ec2.get_instances() 
        for state in self.ec2.list_states(not_matching_instances=instances):
            log.debug("Garbage collect key '%s'..." % state)
            self.ec2.set_state(state, "", TTL=1)

        # Take a snapshot of current knwon scaling states to detect changes
        self.scaling_state_snapshots = {}
        for i in self.ec2.get_instances(State="pending,running"):
            instance_id = i["InstanceId"]
            self.scaling_state_snapshots[instance_id] = self.ec2.get_scaling_state(instance_id, do_not_return_excluded=True)


        # Display a warning if some AZ are manually disabled
        disabled_azs = self.get_disabled_azs()
        if len(disabled_azs) > 0:
            log.info("Some Availability Zones are disabled: %s" % disabled_azs)

        # Register dynamic keys for subfleets
        for subfleet in self.ec2.get_subfleet_names():
            if subfleet in ["__all__"]:
                continue
            extended_metrics = get_subfleet_key("ec2.schedule.metrics.enable", subfleet, cls=int) 
            if extended_metrics:
                log.log(log.NOTICE, f"Enabled detailed metrics for subfleet '{subfleet}' (subfleet.{subfleet}.ec2.schedule.metrics.enable != 0).")
                dimensions = [{
                    "Name": "SubfleetName",
                    "Value": subfleet}]
                self.cloudwatch.register_metric([ 
                        { "MetricName": "EC2.Size",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.ExcludedInstances",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.RunningInstances",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.DrainingInstances",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.NbOfCPUCreditingInstances",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.MinInstanceCount",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.DesiredInstanceCount",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.NbOfInstanceInUnuseableState",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "EC2.NbOfInstanceInInitialState",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                        { "MetricName": "SSM.MaintenanceWindow",
                          "Dimensions": dimensions,
                          "Unit": "Count",
                          "StorageResolution": self.metric_time_resolution },
                    ])



    ###############################################
    #### EVENT GENERATION #########################
    ###############################################

    @xray_recorder.capture()
    def generate_instance_transition_events(self):
        """  Generate events on instance state transition.

        On instance state change (ex: stopped => pending), an event 'instance_transitions' is generated that can 
        be intercepted by users (through a Lambda function, a SNS or a SQS message).
        """
        transitions = []
        for instance in self.instances_wo_excluded: 
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
            # Generate an Event
            R(None, self.instance_transitions, Transitions=transitions)

    def instance_transitions(self, Transitions=None):
        """ This method is only for its signature will be reflected in the generated event.
        """
        return {}



    ###############################################
    #### UTILITY FUNCTIONS ########################
    ###############################################

    def get_min_instance_count(self):
        """ Return the minimum instance count linked to 'ec2.schedule.min_instance_count'.
        Especially, it converts percentage into an absolute number.

        :return An integer (number of instances)
        """
        instances = self.all_main_fleet_instances 
        return max(0, Cfg.get_abs_or_percent("ec2.schedule.min_instance_count", -1, len(instances)))

    def desired_instance_count(self):
        """ Return the desired instance count linked in 'ec2.schedule.desired_instance_count'.
        Especially, it converts percentage into an absolute number.

        :return An integer (number of instances
        """
        instances = self.all_main_fleet_instances 
        return Cfg.get_abs_or_percent("ec2.schedule.desired_instance_count", -1, len(instances))

    def get_ready_for_operation_timeouted_instances(self):
        """ Return a list of instance ids that spent too much time in 'initializing' time.
        """
        if not self.ssm.is_feature_enabled("events.ec2.instance_ready_for_operation"):
            return []
        ids = []
        max_initializing_delay = Cfg.get_duration_secs("ssm.feature.events.ec2.instance_ready_for_operation.max_initializing_time")
        for i in self.pending_running_instances:
            instance_id = i["InstanceId"]
            if self.ec2.is_instance_state(instance_id, ["initializing"]) and self.ec2.is_instance_state(instance_id, ["unhealthy"]):
                ids.append(instance_id)
        return ids

    def get_ready_for_shutdown_timeouted_instances(self):
        """ Return a list of instance ids that spent too much time in 'draining' time.
        """
        if not self.ssm.is_feature_enabled("events.ec2.instance_ready_for_shutdown"):
            return []
        max_shutdown_delay = Cfg.get_duration_secs("ssm.feature.events.ec2.instance_ready_for_shutdown.max_shutdown_delay")
        grace_period       = Cfg.get_duration_secs("ec2.schedule.bounce.instances_with_issue_grace_period")
        ids                = []
        for i in self.pending_running_instances:
            instance_id = i["InstanceId"]
            meta        = {}
            if self.ec2.get_scaling_state(instance_id, do_not_return_excluded=True, meta=meta) == "draining":
                fleet = self.ec2.get_subfleet_name_for_instance(i)
                if instance_id in self.need_cpu_crediting_instance_ids:
                    continue # Do not count CPU crediting instances as faulty
                if meta.get("last_draining_date") is None:
                    continue
                if self.ssm.is_maintenance_time(fleet=fleet):
                    continue
                if (self.context["now"] - meta["last_draining_date"]).total_seconds() > (max_shutdown_delay - grace_period):
                    ids.append(instance_id)
        return ids

    def get_instances_with_issues(self):
        """ Return a list of Instance Id of faulty instances.

        A faulty instance can be any of:
            - Instances that are part of one or more TargetGroups and reported 'unavail' or 'unhealth',
            - Instances that have EC2 status as 'impaired' or 'unhealthy',
            - Instances that have been AZ evicted (either manually or autoamtically due to AWS signalling an AZ is unavailable),
            - Spot instances are signaled and 'rebalance recommended' or 'interrupted',
            - Burstable instances that have CPU Credit exhausted.
        :return A list if instance ids
        """
        active_instances        = self.pending_running_instances
        instances_with_issue_ids= []

        # TargetGroup related issues
        instances_with_issue_ids.extend(self.unhealthy_instances_in_targetgroups)

        # EC2 related issues
        impaired_instances      = [i["InstanceId"] for i in active_instances 
                if self.ec2.is_instance_state(i["InstanceId"], ["impaired", "unhealthy", "az_evicted"]) ]
        [ instances_with_issue_ids.append(i) for i in impaired_instances if i not in instances_with_issue_ids]

        # Interrupted spot instances are 'unhealthy' too
        for i in self.spot_excluded_instance_ids:
            instance = self.ec2.get_instance_by_id(i)
            if instance["State"]["Name"] == "running" and i not in instances_with_issue_ids:
                instances_with_issue_ids.append(i)

        if Cfg.get_int("ec2.schedule.assume_cpu_exhausted_burstable_instances_as_unuseable"):
            # CPU credit "issues"
            exhausted_cpu_instances = sorted([ i["InstanceId"] for i in self.cpu_exhausted_instances])
            all_instances           = self.all_instances
            max_i                   = len(all_instances)
            for i in exhausted_cpu_instances:
                if max_i <= 0:
                    break
                if i in instances_with_issue_ids:
                    continue
                if self.ssm.is_maintenance_time(fleet=self.o_ec2.get_subfleet_name_for_instance(i)):
                    continue
                instances_with_issue_ids.append(i)
                max_i -= 1

        # Add faulty instances that go beyond their time for 'ready_for_operation' and 'ready_for_shutdown' event
        instances_with_issue_ids.extend([i for i in self.ready_for_operation_timeouted_instances if i not in instances_with_issue_ids])
        instances_with_issue_ids.extend([i for i in self.ready_for_shutdown_timeouted_instances  if i not in instances_with_issue_ids])

        return instances_with_issue_ids

    def get_useable_instances(self, instances=None, State="pending,running", ScalingState=None,
            exclude_problematic_instances=True, exclude_bounced_instances=True, 
            exclude_initializing_instances=False, initializing_only=False):
        """ Return a list of Instance full structures according to supplied data selectors.

        :param instances:                       List of instances to filter (if 'None', all instances are considered)
        :param State:                           Instance state selector ("stopped", "pending", "running"...)
        :param ScalingState:                    Instance scaling state selector ("draining", "bounced", "error"...)
        :param exclude_problematic_instances:   Filter out instances that have issues ("unhealthy", "impaired" etc...)
        :param exclude_bounced_instances:       Filter out instances marked with scaling state "bounced"
        :param exclude_initializing_instances:  Filter out instances that are considered as initializing 
            (either because instances are too young, marked as 'initializing' from EC2 PoV or 'initializing' if part of TargetGroup)
        :param initializing_only:               Filter in instances are 'initializing' state
        :return A list of Instance
        """
        if ScalingState is None: ScalingState = "-excluded,draining,error%s" % (",bounced" if exclude_bounced_instances else "")
        active_instances = self.ec2.get_instances(instances=instances, State=State, ScalingState=ScalingState)
        
        instances_ids_with_issues   = self.instances_with_issues 

        if exclude_problematic_instances:
            active_instances = list(filter(lambda i: i["InstanceId"] not in instances_ids_with_issues, active_instances))

        if initializing_only:
            active_instances = list(filter(lambda i: i["InstanceId"] in self.initializing_instances, active_instances))

        if exclude_initializing_instances:
            active_instances = list(filter(lambda i: i["InstanceId"] not in self.initializing_instances, active_instances))

        return active_instances

    def get_useable_instance_count(self, exclude_problematic_instances=True, exclude_bounced_instances=True, 
            exclude_initializing_instances=False, initializing_only=False):
        """ Return a number of usueable instance.

        :param exclude_problematic_instances:   Filter out instances that have issues ("unhealthy", "impaired" etc...)
        :param exclude_bounced_instances:       Filter out instances marked with scaling state "bounced"
        :param exclude_initializing_instances:  Filter out instances that are considered as initializing 
            (either because instances are too young, marked as 'initializing' from EC2 PoV or 'initializing' if part of TargetGroup)
        :param initializing_only:               Filter in instances are 'initializing' state
        :return An integer
        """ 
        return len(self.get_useable_instances(exclude_problematic_instances=exclude_problematic_instances, 
                exclude_bounced_instances=exclude_bounced_instances, exclude_initializing_instances=exclude_initializing_instances,
                initializing_only=initializing_only))

    def get_cpu_exhausted_instances(self, threshold=1):
        """ Return list of instances that have their CPU exhausted below the specified threshold

        :param threshold: A pourcentage of CPU Credit to consider the minimum required
        :return A list of instance structures
        """
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

    def get_young_instance_ids(self, instances=None):
        """ Return a list of instance that are considered young based on their running time.

        Instances that are running for less than duration specified in 'ec2.schedule.start.warmup_delay'.

        :return A list of instances.
        """
        now                     = self.context["now"]
        warmup_delay            = Cfg.get_duration_secs("ec2.schedule.start.warmup_delay")
        active_instances        = self.pending_running_instances_wo_excluded if instances is None else instances
        return [ i["InstanceId"] for i in active_instances if (now - i["LaunchTime"]).total_seconds() < warmup_delay]

    def get_initial_instances(self, instances=None):
        """ Return list of instances in 'initializing' state.

        'Initializing' status is aither:
            - Marked as such at EC2 level,
            - At least on TargetGroup is currently initializing the instance,
            - Running not longer enough.

        :param A list of instance structures.
        """
        active_instances        = self.pending_running_instances_wo_excluded if instances is None else instances
        active_instance_ids     = [i["InstanceId"] for i in active_instances]
        initializing_instances  = []
        initializing_instances.extend([ i["InstanceId"] for i in active_instances if self.ec2.is_instance_state(i["InstanceId"], ["initializing"]) ])
        registered_instance_ids = self.targetgroup.get_registered_instance_ids(state="initial")
        initializing_instances.extend([i for i in registered_instance_ids if i in active_instance_ids])
        young_instances         = self.get_young_instance_ids(instances=active_instances)
        return list(filter(lambda i: i["InstanceId"] in young_instances or i["InstanceId"] in initializing_instances, active_instances))

    def get_initial_instances_ids(self):
        """ Return the list of 'initializing' instance ids.
        """
        return [i["InstanceId"] for i in self.initializing_instances]

    def get_disabled_azs(self):
        """ Return the list of AZ names that must not be scheduled. 
        """
        return self.ec2.get_azs_with_issues()

    def set_state(self, key, value, TTL=None):
        """ Helper method to set state with the default module TTL.
        """
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
        if not Cfg.get_int("ec2.schedule.disable"):
            self.shelve_extra_lighthouse_instances()
            self.scale_desired()
            self.scale_bounce()
            self.scale_bounce_instances_with_issues()
            self.scale_in_out()
        self.wakeup_burstable_instances()
        self.manage_excluded_instances()
        self.manage_subfleets()
        self.generate_subfleet_dashboard()
        self.stop_drained_instances()

    @xray_recorder.capture()
    def send_events(self):
        """ Send SSM Events linked to instance state changes.
        """
        o_ssm = self.context["o_ssm"]
        if o_ssm.is_feature_enabled("events.ec2.scaling_state_changes"):
            ids_per_new_state = {}
            for instance_id in self.scaling_state_snapshots:
                previous_state = self.scaling_state_snapshots[instance_id]
                current_state  = self.ec2.get_scaling_state(instance_id, do_not_return_excluded=True)
                if previous_state != current_state:
                    if current_state not in ids_per_new_state:
                        ids_per_new_state[current_state] = {}
                    if previous_state not in ids_per_new_state[current_state]:
                        ids_per_new_state[current_state][previous_state] = []
                    ids_per_new_state[current_state][previous_state].append({"InstanceId": instance_id}) 

            event_name_mappings = {
                    "draining": {
                        "Name": "INSTANCE_SCALING_STATE_DRAINING",
                        "PrettyName": "InstanceScalingState-Draining"
                    },
                    "bounced": {
                        "Name": "INSTANCE_SCALING_STATE_BOUNCED",
                        "PrettyName": "InstanceScalingState-Bounced"
                    },
                }

            draining_ids = []
            for state in ids_per_new_state:
                if state in event_name_mappings:
                    for previous_state in ids_per_new_state[state]:
                        change            = ids_per_new_state[state][previous_state]
                        instance_ids      = [c["InstanceId"] for c in change]
                        if state == "draining":
                            draining_ids.extend(instance_ids)
                        log.log(log.NOTICE, f"Instances {instance_ids} changed their scaling state from '{previous_state}' to '{state}'.")
                        event_name        = event_name_mappings[state]["Name"]
                        pretty_event_name = event_name_mappings[state]["PrettyName"]
                        args              = {
                            "NewState": state,
                            "OldState": previous_state,
                        }
                        o_ssm.send_events(instance_ids, "ec2.scaling_state.change", event_name, args, pretty_event_name=pretty_event_name)

            # Send the BlockNewConnectionsToPorts event if needed
            ids_per_fleet = defaultdict(list)
            for i in draining_ids:
                ids_per_fleet[self.ec2.get_subfleet_name_for_instance(i)].append(i)

            for fleet in ids_per_fleet:
                ids = ids_per_fleet[fleet]
                if fleet is None: fleet = "__main__"
                blocked_ports = Cfg.get_list("ssm.feature.events.ec2.scaling_state_changes."
                        f"draining.{fleet}.connection_refused_tcp_ports", default=[])
                if not len(blocked_ports):
                    continue
                args = {
                    "BlockedPorts": " ".join(blocked_ports)
                }
                log.info(f"Sending BlockNewConnectionsToPorts SSM Event to '{fleet}' fleet instances (BlockedPorts={blocked_ports})...")
                o_ssm.send_events(ids, "ec2.scaling_state.change.draining.block_new_connections", 
                    "INSTANCE_BLOCK_NEW_CONNECTIONS_TO_PORTS", args, pretty_event_name="BlockNewConnectionsToPorts")
        
    @xray_recorder.capture()
    def prepare_metrics(self):
        """ Compute all module CloudWatch metrics and Synthetic metrics available through the API Gateway.
        """
        cw = self.cloudwatch
        fleet_instances             = self.all_main_fleet_instances
        draining_instances          = self.pending_running_instances_draining_wo_excluded
        running_instances           = self.running_instances_wo_excluded
        pending_instances           = self.pending_instances_wo_draining_excluded
        stopped_instances           = self.stopped_instances_wo_excluded
        stopping_instances          = self.stopping_instances_wo_excluded
        excluded_instances          = self.excluded_instances
        bounced_instances           = self.pending_running_instances_bounced_wo_excluded
        error_instances             = self.error_instances
        exhausted_cpu_credits       = self.cpu_exhausted_instances
        main_fleet_instance_ids     = [i["InstanceId"] for i in self.all_main_fleet_instances]
        instances_with_issues       = [i for i in self.instances_with_issues if i in main_fleet_instance_ids]
        subfleet_instances          = self.subfleet_instances_w_excluded
        running_subfleet_instances  = self.running_subfleet_instances
        draining_subfleet_instances = self.draining_subfleet_instances
        fl_size                     = len(fleet_instances)
        cw.set_metric("FleetSize",             len(fleet_instances) if fl_size > 0 else None)
        cw.set_metric("DrainingInstances",     len(draining_instances) if fl_size > 0 else None)
        cw.set_metric("RunningInstances",      len(running_instances) if fl_size > 0 else None)
        cw.set_metric("PendingInstances",      len(pending_instances) if fl_size > 0 else None)
        cw.set_metric("StoppedInstances",      len(stopped_instances) if fl_size > 0 else None)
        cw.set_metric("StoppingInstances",     len(stopping_instances) if fl_size > 0 else None)
        cw.set_metric("MinInstanceCount",      self.get_min_instance_count() if fl_size > 0 else None)
        cw.set_metric("DesiredInstanceCount",  max(self.desired_instance_count(), 0) if fl_size > 0 else None)
        cw.set_metric("NbOfExcludedInstances", len(excluded_instances) - len(subfleet_instances) if fl_size else None)
        cw.set_metric("NbOfBouncedInstances",  len(bounced_instances) if fl_size > 0 else None)
        cw.set_metric("NbOfInstancesInError",  len(error_instances) if fl_size > 0 else None)
        cw.set_metric("InstanceScaleScore",    self.instance_scale_score if fl_size > 0 else None)
        cw.set_metric("RunningLighthouseInstances", len(self.get_lighthouse_instance_ids(running_instances)) if fl_size > 0 else None)
        cw.set_metric("NbOfInstanceInInitialState", len(self.get_initial_instances()) if fl_size > 0 else None)
        cw.set_metric("NbOfInstanceInUnuseableState", len(instances_with_issues) if fl_size > 0 else None)
        cw.set_metric("NbOfCPUCreditExhaustedInstances", len(exhausted_cpu_credits) if fl_size > 0 else None)
        if self.ssm.is_feature_enabled("maintenance_window"):
            cw.set_metric("SSM.MaintenanceWindow", self.ssm.is_maintenance_time(fleet=None))
        else:
            cw.set_metric("SSM.MaintenanceWindow", None)
        subfleet_count = len(subfleet_instances)
        # Send metrics only if there are fleet instances
        cw.set_metric("Subfleet.EC2.Size", len(subfleet_instances) if subfleet_count else None)
        cw.set_metric("Subfleet.EC2.RunningInstances", len(running_subfleet_instances) if subfleet_count else None)
        cw.set_metric("Subfleet.EC2.DrainingInstances", len(draining_subfleet_instances) if subfleet_count else None)

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
            log.info("InstanceConditions > These instances are in 'ERROR' state: %s" % [i["InstanceId"] for i in error_instances])
        if len(self.unhealthy_instances_in_targetgroups):
            log.info(f"InstanceConditions > These instances are reported 'unhealthy' in at least one targetgroup: %s" % 
                    self.unhealthy_instances_in_targetgroups)
        if len(self.spot_rebalance_recommanded_ids):
            log.info(f"InstanceConditions > These Spot instances in 'rebalance recommanded' state: %s" % self.spot_rebalance_recommanded_ids)
        if len(self.spot_interrupted_ids):
            log.info(f"InstanceConditions > These Spot instances in 'interrupted' state: %s" % self.spot_interrupted_ids)
        if len(self.ready_for_operation_timeouted_instances):
            log.info(f"InstanceConditions > These instances waited too long to send 'ready-for-operation' SSM status: %s" % 
                    self.ready_for_operation_timeouted_instances)
        if len(self.ready_for_shutdown_timeouted_instances):
            log.info(f"InstanceConditions > These instances waited too long to send 'ready-for-shutdown' SSM status: %s" % 
                    self.ready_for_shutdown_timeouted_instances)
        if len(self.need_cpu_crediting_instance_ids):
            log.info(f"InstanceConditions > These instances are CPU Crediting (long draining time to win burstable CPU credits): %s" % 
                    self.need_cpu_crediting_instance_ids)
        if len(self.instances_with_issues): 
            log.info("InstanceConditions > These instances are 'unuseable' (all causes: unavail/unhealthy/impaired/spotinterrupted/lackofcpucredit...) : %s" % 
                self.instances_with_issues)

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
            "Subfleets" : []
        }
        subfleet_stats = s_metrics["Subfleets"]
        for subfleet in self.ec2.get_subfleet_names():
            stats = {
                    "Name": subfleet,
                    "RunningInstances": [],
                    "RunningInstanceCount": 0,
                    "StoppedInstances": [],
                    "StoppedInstanceCount": 0,
                    "SubfleetSize": 0
                }
            fleet = self.ec2.get_subfleet_instances(subfleet_name=subfleet)
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
        """ Return synthetics metrics (used by the API Gateway statistic methods.
        """
        return self.synthetic_metrics


    ###############################################
    #### LOW LEVEL INSTANCE HANDLING ##############
    ###############################################

    def sort_and_filter_stopped_instance_candidates(self, active_instances, stopped_instances):
        """ This method is used to sort and filter instances according to horizontal and vertical scaling algorithms.

        This method is used both for Main and Subfleets to define the "best" order to start instances when needed.
            - It filters out instances that are notified with Spot 'rebalance_recommended' and 'interrupted' status,
            - If any, it filters out instance type and AZ couples that are marked as unschedulable (see compute_spot_exclusion_lists()),
            - If 'ec2.schedule.spot.min_stop_period' is different than 0, recntly stopped Spot instances are filtered out,
            - The latest action is to sort instances to ensure AZ fait balancing.

        :param active_instances: A list of running instances that will be used to define the right instance balacing between AZs,
        :param stopped_instances: A list of stopped instances to sort and filter as starteable candidates.
        :return A sorted and filtered list of instances.
        """
        # Filter out instances that are not startable
        stopped_instances   = list(filter(lambda i: i["InstanceId"] not in self.unstartable_ids, stopped_instances))
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
        """ Return a list of startable instances (in the autoscaled fleet).

        Filter and sort instance candidates to be started in 'scale up' event in the Main subfleet.

        :param caller:                  The name of the module algorithm requesting a scaleout.
        :param expected_instance_count: The absolute number of expected serving instances
        :param target_for_dispatch:     A structure passing the expected configuration for LightHouse instance count mainly.
        :param A filtered and sorted list of instances
        """
        disabled_azs     = self.get_disabled_azs()
        active_instances = self.pending_running_instances_wo_excluded_draining_error 
        # Get all stopped instances
        stopped_instances = self.ec2.get_instances(State="stopped", ScalingState="-excluded", azs_filtered_out=disabled_azs)

        # Get a list of startable instances
        stopped_instances = self.sort_and_filter_stopped_instance_candidates(active_instances, stopped_instances)

        # Let the scaleout algorithm influence the instance selection
        stopped_instances = self.scaleup_sort_instances(stopped_instances, 
                target_for_dispatch if target_for_dispatch is not None else expected_instance_count, 
                caller)
        return stopped_instances

    def filter_running_instance_candidates(self, active_instances):
        """ Return a list of stoppable instances in the autoscaled fleet.

        :param active_instances: List of running instances to filter and sort as candidates for stop
        :return A list of stoppable instances
        """
        # Filter out instances that are not stoppable
        active_instances = list(filter(lambda i: i["InstanceId"] not in self.unstoppable_ids, active_instances))

        # Ensure we picked instances in a way that keep AZs balanced
        active_instances = self.ec2.sort_by_balanced_az(active_instances, active_instances, smallest_to_biggest_az=False)

        # If we have disabled AZs, we placed instances part of them in front of list to remove associated instances first
        active_instances = self.ec2.sort_by_prefered_azs(active_instances, prefered_azs=self.get_disabled_azs()) 

        # We place instances with unuseable status in front of the list
        active_instances = self.ec2.sort_by_prefered_instance_ids(active_instances, prefered_ids=self.instances_with_issues) 

        # Put interruped Spot instances as first candidates to stop
        active_instances = self.ec2.filter_spot_instances(active_instances, EventType="+rebalance_recommended",
                filter_in_instance_types=self.excluded_spot_instance_types, merge_matching_spot_first=True)
        active_instances = self.ec2.filter_spot_instances(active_instances, EventType="+interrupted",
                filter_in_instance_types=self.excluded_spot_instance_types, merge_matching_spot_first=True)
        return active_instances


    @xray_recorder.capture()
    def instance_action(self, desired_instance_count, caller, reject_if_initial_in_progress=False, target_for_dispatch=None):
        """ Perform action (start or stop instances) needed to achieve specified 'desired_instance_count' in the Main fleet.

        :param desired_instance_count:          The absolute number of instances expected to be serving.
        :param caller:                          The name of the algorithm asking for a change in the number of instance serving.
        :param reject_if_initial_in_progress:   If 'True', the method refuses to make any change is there is at least one instance 
            in 'initializing' state.
        :param target_for_dispatch:             Parameter specifically linked to expected state of 'LightHouse' instance count.
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
            log.debug("Instance_action (%d) from '%s'... " % (delta_instance_count, caller))

        if delta_instance_count > 0:
            # Request to add new running instances (scaleout)
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
            # Request to remove running instances (scalein)
            c = min(-delta_instance_count, Cfg.get_int("ec2.schedule.max_instance_stop_at_a_time"))
            if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=None):
                log.info(f"Scale-in actions disabled during Main fleet SSM Maintenance Window: "
                    "Should have placed in 'draining' state up to {c} instances...")
                return False

            # We need to assume that instances in a 'initializing' state
            #    can't be stated as useable instances yet. We usually prefer to delay downsize decision until there 
            #    are not more instance in 'initializing' state to take precise decisions.
            if reject_if_initial_in_progress:
                initial_target_instance_ids = self.get_initial_instances_ids()
                log.log(log.NOTICE, "Number of instance targets in 'initial' state: %d" % len(initial_target_instance_ids))
                if len(initial_target_instance_ids) > 0:
                    log.log(log.NOTICE, "Some targets are still initializing. Do not consider stopping instances now...")
                    return False

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
        """ Return a boolean if the specified instance is eligible to the CPU Crediting mechanism.
        """
        instance_type = i["InstanceType"]
        if instance_type not in self.cpu_credits or instance_type.startswith("t2"):
            return False # Not a burstable or not eligible instance
        return True

    def is_instance_need_cpu_crediting(self, i, meta):
       """ Return True if the specified instance need CPU Crediting.

       :param i:       An instance structure
       :param meta:    If not 'None', a dict populated with Metadata
       :return True if the specified instance need CPU Crediting.:w
       """
       now           = self.context["now"]
       instance_id   = i["InstanceId"]

       instance_type = i["InstanceType"]
       if not self.is_instance_cpu_crediting_eligible(i):
           return False 

       # This instance to stop is a burstable one
       stopped_instances    = self.stopped_instances_wo_excluded_error

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
           log.info("'%s' is CPU crediting... (is_subfleet_instance=%s, current_credit=%.2f, minimum_required_credit=%s, maximum_possible_credit=%s, %s)" % 
                   (instance_id, self.ec2.is_subfleet_instance(instance_id), cpu_credit, min_cpu_credit_required, max_earned_credits, instance_type))
           return True
       return False

    def compute_cpu_crediting_instances(self, mainfleet_crediting_instance_ids, subfleet_crediting_instances):
        instances                      = self.pending_running_instances_draining
        # Variable for autoscale fleet management
        non_burstable_instances        = self.non_burstable_instances
        max_number_crediting_instances = Cfg.get_abs_or_percent("ec2.schedule.burstable_instance.max_cpu_crediting_instances", -1, 
                len(self.instances_wo_excluded))

        subfleet_details = defaultdict(dict)
        for subfleet in self.ec2.get_subfleet_names():
            subfleet_details[subfleet]["Instances"]                      = self.ec2.get_subfleet_instances(subfleet_name=subfleet)
            subfleet_details[subfleet]["max_number_crediting_instances"] = get_subfleet_key_abs_or_percent(
                    "ec2.schedule.burstable_instance.max_cpu_crediting_instances", subfleet, "50%", len(subfleet_details[subfleet]["Instances"]))
            subfleet_details[subfleet]["cpu_credit_counter"]             = subfleet_details[subfleet]["max_number_crediting_instances"]

        for i in instances:
            instance_id   = i["InstanceId"]
            subfleet_name = self.ec2.get_subfleet_name_for_instance(i)
            if self.is_instance_cpu_crediting_eligible(i):
                meta      = {}
                if self.ec2.get_scaling_state(instance_id, meta=meta) in ["draining"] and self.is_instance_need_cpu_crediting(i, meta):
                    if subfleet_name is not None:
                        # Subfleet
                        if subfleet_details[subfleet_name]["cpu_credit_counter"]:
                            subfleet_details[subfleet_name]["cpu_credit_counter"] -= 1
                            subfleet_crediting_instances[subfleet_name].append(instance_id)
                    else:
                        # Main fleet
                        mainfleet_crediting_instance_ids.append(instance_id)
        crediting_instance_ids = mainfleet_crediting_instance_ids[:max_number_crediting_instances]
        for subfleet in subfleet_crediting_instances:
            crediting_instance_ids.extend(subfleet_crediting_instances[subfleet])
        return crediting_instance_ids


    @xray_recorder.capture()
    def stop_drained_instances(self):
        """ Method responsible to stop instance marked as 'draining'.
        """
        now              = self.context["now"]
        cw               = self.cloudwatch

        draining_target_instance_ids = self.targetgroup.get_registered_instance_ids(state="draining")
        if len(draining_target_instance_ids):
            registered_targets = self.targetgroup.get_registered_targets(state="draining")
            log.debug("Registered targets: %s " % Dbg.pprint(registered_targets))

        # Prepare subfleet details
        subfleet_details = defaultdict(dict)
        for subfleet in self.ec2.get_subfleet_names():
            subfleet_details[subfleet]["IsRunningState"]                 = get_subfleet_key("state", subfleet, none_on_failure=True) == "running"
            subfleet_details[subfleet]["Instances"]                      = self.ec2.get_subfleet_instances(subfleet_name=subfleet)
            subfleet_details[subfleet]["desired_instance_count"]         = get_subfleet_key("ec2.schedule.desired_instance_count", subfleet)

        # Retrieve list of instance marked as draining and running
        instances     = self.pending_running_instances_draining
        instances_ids = self.ec2.get_instance_ids(instances) 

        max_number_crediting_instances = Cfg.get_abs_or_percent("ec2.schedule.burstable_instance.max_cpu_crediting_instances", -1,
                         len(self.instances_wo_excluded))
        max_startable_stopped_instances= self.filter_autoscaled_stopped_instance_candidates("stop_drained_instances", len(self.useable_instances))
        # To avoid availability issues, we do not allow more cpu crediting instances than startable instances
        #    It ensures that burstable instances are still available *even CPU Exhausted* into the fleet.
        max_number_crediting_instances = min(max_number_crediting_instances, len(max_startable_stopped_instances) 
                + len(self.draining_lighthouse_instances_ids)) # We add the number of draining LH instances as they may be in CPU crediting state and
                                                               # can't be part of a full scaleout sequence (so are useless to be rendered available)
        mainfleet_cpu_crediting_instance_ids = self.need_mainfleet_cpu_crediting_ids[:max_number_crediting_instances]

        # Variable for autoscale fleet management
        ids_to_stop                    = []
        ssm_ready_for_shutdown_delay   = Cfg.get_duration_secs("ssm.feature.events.ec2.instance_ready_for_shutdown.max_shutdown_delay")
        cooldown                       = Cfg.get_duration_secs("ec2.schedule.draining.instance_cooldown")
        for i in instances:
           instance_id = i["InstanceId"]
           # Refresh the TTL if the 'draining' operation takes a long time
           #   and collect metadata about the instance scaling state.
           subfleet_name = self.ec2.get_subfleet_name_for_instance(i)
           meta          = {}
           self.ec2.set_scaling_state(instance_id, "draining", meta=meta, 
                   force=self.ssm.is_maintenance_time(fleet=subfleet_name)) # Fore update of 'last_draining_time' when in maintenance

           if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=subfleet_name):
               log.info(f"Can't stop drained instance {instance_id} while a SSM Maintenance window is active.")
               continue
            
           if instance_id in self.subfleet_cpu_crediting_ids[subfleet_name]:
               if subfleet_details[subfleet_name]["IsRunningState"] and subfleet_details[subfleet_name]["desired_instance_count"] == "100%":
                  pass # When desired_instance_count is set to 100%, we want all instances up and running as fast as possible
                       #   so we prematuraly exit currently CPU Crediting instances.
               else:
                  letter_box = self.letter_box_subfleet_to_stop_drained_instances
                  # Did we receive a letter from subfleet management method?
                  if letter_box[subfleet_name]: 
                       # The subfleet management method wants stop_drained_instances to shutdown some instances
                       #   to bring them back into the serving pool ASAP.
                       letter_box[subfleet_name] -= 1
                  else:
                     continue

           if instance_id in mainfleet_cpu_crediting_instance_ids and Cfg.get("ec2.schedule.desired_instance_count") != "100%":
               continue # When desired_instance_count is set to 100%, we want all instances up and running as fast as possible
                    #   so we prematuraly exit currently CPU Crediting instances.

           if instance_id in draining_target_instance_ids:
               log.info(f"Can't stop yet instance {instance_id}. Target Group is still draining it...")
               continue

           if instance_id in self.unstoppable_ids:
               log.info(f"Can't stop yet instance {instance_id} as marked as unstoppable...")
               continue

           if self.targetgroup.is_instance_registered(None, instance_id):
               log.log(log.NOTICE, "Instance %s if still part of a Target Group. Wait for eviction before to stop it..." % instance_id)
               continue

           draining_date = meta["last_draining_date"]
           if draining_date is None:
               log.warning(f"No 'last_draining_date' for instance {instance_id}. Bug??")
           else:
               elapsed_time  = now - draining_date
               if elapsed_time < timedelta(seconds=cooldown):
                   log.log(log.NOTICE, "Instance '%s' is still in draining cooldown period (elapsed_time=%d, ec2.schedule.draining.instance_cooldown=%d): "
                        "Do not assess stop now..." % (instance_id, elapsed_time.total_seconds(), cooldown))
                   continue

               if (self.ssm.is_feature_enabled("events.ec2.instance_ready_for_shutdown") and 
                       elapsed_time < timedelta(seconds=ssm_ready_for_shutdown_delay)):
                   # Check SSM based instance-ready-for-shutdown status
                   ssm_hcheck = self.ssm.run_command([instance_id], "INSTANCE_READY_FOR_SHUTDOWN", 
                           comment="CS-InstanceReadyForShutdown (%s)" % self.context["GroupName"], return_former_results=True)
                   status     = ssm_hcheck[instance_id]["Status"] if instance_id in ssm_hcheck else ""
                   if status == "FAILURE":
                       log.log(log.NOTICE, f"Instance '{instance_id}' is reporting that it is NOT yet ready to shutdown now...")
                       continue
                   elif status not in ["SUCCESS"]:
                       log.log(log.NOTICE, f"Instance '{instance_id}' is waiting for ready-for-shutdown SSM status (elapsed_time=%d, "
                            f"ec2.schedule.draining.ssm_ready_for_shutdown_delay={ssm_ready_for_shutdown_delay}): "
                            "Do not assess stop now..." % elapsed_time.total_seconds())
                       continue

           ids_to_stop.append(instance_id)

        # Configure NbOfCPUCreditingInstances metric
        burstable_instances = [i for i in self.instances_wo_excluded if self.is_instance_cpu_crediting_eligible(i)]
        cw.set_metric("NbOfCPUCreditingInstances", len(mainfleet_cpu_crediting_instance_ids) if len(burstable_instances) else None)
        for subfleet in self.ec2.get_subfleet_names():
            dimensions = [{
                "Name": "SubfleetName",
                "Value": subfleet}]
            ins                 = subfleet_details[subfleet]["Instances"]
            burstable_instances = [i for i in ins if self.is_instance_cpu_crediting_eligible(i)]
            self.cloudwatch.set_metric("EC2.NbOfCPUCreditingInstances", 
                    len(self.subfleet_cpu_crediting_ids[subfleet]) if len(burstable_instances) else None, dimensions=dimensions)

        if len(ids_to_stop) > 0:
            if self.scaling_state_changed:
                log.debug("Instance state changed. Do stop instance stop now...")
                return
            self.ec2.stop_instances(ids_to_stop)
            log.info("Sent stop request for instances %s." % ids_to_stop)
            self.scaling_state_changed = True

    def wakeup_burstable_instances(self):
        """ Start burstable instances that are stopped for a long time and could lose their CPU Credits soon (near one week stopped).
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

    def subfleet_action(self, subfleet, delta):
        """ Method responsable to change the amount of running instances is a subfleet.

        It is similar to instance_action() but to manage subfleet instance count.
        The method is responsible to start/stop instances based on vertical scaling policy and instance conditions ('initializing', too young etc...)

        :param subfleet:    Name of the subfleet to manage
        :param delta:       Number of instances to start or stop in the subfleet
        """
        def _verticalscale_sort_and_warn(subfleet, candidates, reverse=False):
            """ Take into account vertical scaling policy if it exists for the subfleet.
                Warn the user if inconsistencies are detected.
            """
            vertical_sorted_instances = self.verticalscaling_sort_instances(
                    get_subfleet_key("ec2.schedule.verticalscale.instance_type_distribution", subfleet), 
                    candidates, reverse=reverse)
            candidates = []
            if not reverse:
                # Candidates instance matching the policy will be listed first
                candidates.extend(vertical_sorted_instances["non_lh_instances"])
                candidates.extend(vertical_sorted_instances["non_lh_instances_not_matching_policy"])
            else:
                # Candidates instance NOT matching the policy will be listed first
                candidates.extend(vertical_sorted_instances["non_lh_instances_not_matching_policy"])
                candidates.extend(vertical_sorted_instances["non_lh_instances"])

            if len(vertical_sorted_instances["directives"]) and len(vertical_sorted_instances["non_lh_instances_not_matching_policy"]):
                log.warning(f"Instances %s in subfleet {subfleet} do not match the vertical policy specified in "
                        f"`subfleet.{subfleet}.ec2.schedule.verticalscale.instance_type_distribution`. They will be managed as "
                        "low priority. Please adjust your vertical policy or instance type to allow vertical scaler normal operations." %
                        ([(i["InstanceId"], i["InstanceType"]) for i in vertical_sorted_instances["non_lh_instances_not_matching_policy"]]))
            if len(vertical_sorted_instances["lh_instances"]):
                log.warning(f"Instances %s in subfleet {subfleet} are marked as LightHouse in the vertical policy specified in "
                        f"`subfleet.{subfleet}.ec2.schedule.verticalscale.instance_type_distribution`. LightHouse instances are not "
                        "supported in subfleets and will be ignored/non scheduled." % 
                        ([i["InstanceId"] for i in vertical_sorted_instances["lh_instances"]]))
            return candidates
        
        fleet_instances        = self.ec2.get_subfleet_instances(subfleet_name=subfleet, with_excluded_instances=True) 
        min_instance_count     = max(0, get_subfleet_key_abs_or_percent("ec2.schedule.min_instance_count", subfleet,
                                        0, len(fleet_instances)))
        desired_instance_count = max(0, get_subfleet_key_abs_or_percent("ec2.schedule.desired_instance_count", subfleet,
                                        len(fleet_instances), len(fleet_instances)))

        instance_count    = max(min_instance_count, desired_instance_count)
        running_instances = self.ec2.get_instances(instances=fleet_instances, 
                State="pending,running", ScalingState="-error,draining,bounced")
        running_instances = self.ec2.filter_out_excluded_instances(running_instances)
        stopped_instances = self.ec2.get_instances(instances=fleet_instances, State="stopped")
        stopped_instances = self.ec2.filter_out_excluded_instances(stopped_instances)
        delta             = instance_count - len(running_instances) + delta
        if delta > 0:
            # Request to add new running instances.

            # Sort candidates to start based on the vertical policy
            candidates = _verticalscale_sort_and_warn(subfleet, stopped_instances)
            # Sort again to respect AZ balancing especially
            candidates = self.sort_and_filter_stopped_instance_candidates(running_instances, candidates) 
            instances_to_start = [ i["InstanceId"] for i in candidates ]
            if delta > len(instances_to_start):
                # If we can't start the request number of instances, we set a letter box variable
                #   to ask stop_drained_instances() to release immediatly this amount of 'draining' 
                #   instances if possible
                missing_count = delta - len(instances_to_start)
                log.info("Require %d more subfleet '%s' instances! Try to forcibly stop instances in 'cpu crediting' state..." 
                        % (missing_count, subfleet))
                self.letter_box_subfleet_to_stop_drained_instances[subfleet] = delta - len(instances_to_start)
            if len(instances_to_start):
                log.info(f"Starting up to {delta} subfleet instance(s) (fleet={subfleet})...")
                self.ec2.start_instances(instances_to_start, max_started_instances=delta)
                self.scaling_state_changed = True
        if delta < 0:
            # Request to stop running instances.
            now                    = self.context["now"]
            warmup_delay           = Cfg.get_duration_secs("ec2.schedule.start.warmup_delay")
            initializing_instances = []
            for i in running_instances:
                if self.ec2.is_instance_state(i["InstanceId"], ["initializing"]) or (now - i["LaunchTime"]).total_seconds() < warmup_delay:
                    initializing_instances.append(i)

            if len(initializing_instances):
                log.info(f"Instances %s in subfleet '{subfleet}' are still initializing... Can not drain any new instances now..." %
                        [i["InstanceId"] for i in initializing_instances])
            else:
                candidates             = _verticalscale_sort_and_warn(subfleet, running_instances, reverse=True)
                # Sort again to respect AZ balancing especially
                candidates             = self.filter_running_instance_candidates(candidates)
                if len(candidates):
                    instances_to_stop = [i["InstanceId"] for i in candidates][:-delta]
                    if len(instances_to_stop):
                        if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=subfleet):
                            log.info(f"Scale-in actions disabled during '{subfleet}' subfleet SSM Maintenance Window: "
                                "Should have placed in 'draining' state up to %s instances..." % len(instances_to_stop))
                        else:
                            log.info(f"Draining '{subfleet}' subfleet instance(s) '{instances_to_stop}'...")
                            for instance_id in instances_to_stop:
                                self.ec2.set_scaling_state(instance_id, "draining")
        return (min_instance_count, desired_instance_count)

    def manage_subfleets(self):
        """ Module entrypoint for subfleet instance management.
        """
        instances = self.subfleet_instances_w_excluded
        subfleets = {}
        for i in instances:
            instance_id    = i["InstanceId"]
            subfleet_name  = self.ec2.get_subfleet_name_for_instance(i)
            forbidden_chars = "[ .]"
            if re.match(forbidden_chars, subfleet_name):
                log.warning("Instance '%s' contains invalid characters (%s)!! Ignore this instance..." % (instance_id, forbidden_chars))
                continue
            expected_state = get_subfleet_key("state", subfleet_name, none_on_failure=True)
            if expected_state is None:
                log.log(log.NOTICE, "Encountered a subfleet instance (%s) without state directive. Please set 'subfleet.%s.state' configuration key..." % 
                        (instance_id, subfleet_name))
                continue
            log.debug("Manage subfleet instance '%s': subfleet_name=%s, expected_state=%s" % (instance_id, subfleet_name, expected_state))

            allowed_expected_states = ["running", "stopped", "undefined", ""]
            if expected_state not in allowed_expected_states:
                log.warning("Expected state '%s' for subfleet '%s' is not valid : (not in %s!)" % (expected_state, subfleet_name, allowed_expected_states))
                continue

            if subfleet_name not in subfleets:
                subfleets[subfleet_name] = defaultdict(list)
                subfleets[subfleet_name]["size"] = 0
            subfleets[subfleet_name]["expected_state"] = expected_state
            subfleets[subfleet_name]["size"] += 1

            if self.ec2.is_instance_excluded(i):
                continue # Ignore instances marked as excluded

            if expected_state == "running":
                subfleets[subfleet_name]["ToStart"].append(i)

            if (expected_state == "stopped" and i["State"]["Name"] in ["pending", "running"]):
                subfleets[subfleet_name]["ToStop"].append(i)

            subfleets[subfleet_name]["All"].append(i)

        # Manage start/stop of 'running' subfleet
        for subfleet in subfleets:
            fleet                  = subfleets[subfleet]
            expected_state         = fleet["expected_state"]
            if expected_state in ["undefined", ""]:
                log.info(f"/!\ Subfleet '{subfleet}' is in 'undefined' state. No subfleet scaling action will be performed until "
                    f"subfleet.{subfleet}.state is set to 'running'! (subfleet.{subfleet}.ec2.schedule.min_instance_count and "
                    f"subfleet.{subfleet}.ec2.schedule.desired_instance_count are ignored.)")
                continue

            if expected_state == "stopped":
                instance_ids = [i["InstanceId"] for i in fleet["ToStop"]]
                if len(instance_ids):
                    if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=subfleet):
                        log.info(f"Scale-in actions disabled during '{subfleet}' subfleet SSM Maintenance Window: "
                            "Should have placed in 'draining' state up to %s instances..." % len(instances_to_stop))
                        continue
                    log.info(f"Draining instance(s) '{instance_ids}' from 'stopped' fleet '{subfleet}'...")
                for instance_id in instance_ids:
                    self.ec2.set_scaling_state(instance_id, "draining")

            if expected_state == "running":
                # Ensure that the right number of instances are started
                min_instance_count, desired_instance_count = self.subfleet_action(subfleet, 0)

            # Publish subfleet metrics if requested
            dimensions = [{
                "Name": "SubfleetName",
                "Value": subfleet}]
            cw = self.cloudwatch
            if Cfg.get_int(f"subfleet.{subfleet}.ec2.schedule.metrics.enable"):
                running_instances  = self.ec2.get_instances(instances=fleet["All"], 
                        State="pending,running", ScalingState="-error")
                running_instances  = self.ec2.filter_out_excluded_instances(running_instances)
                initial_instances  = self.get_initial_instances(instances=running_instances)
                draining_instances = self.ec2.get_instances(instances=fleet["All"], 
                        State="pending,running", ScalingState="draining")
                draining_instances  = self.ec2.filter_out_excluded_instances(draining_instances)
                subfleet_instances_w_excluded = self.ec2.get_subfleet_instances(subfleet_name=subfleet, 
                        with_excluded_instances=True)
                subfleet_faulty_instance_ids_w_excluded = [i["InstanceId"] for i in running_instances 
                        if i["InstanceId"] in self.instances_with_issues]
                fleet_size             = fleet["size"]
                fleet_size_wo_excluded = len(fleet["All"]) 
                excluded_count = len(subfleet_instances_w_excluded) - fleet_size_wo_excluded
                cw.set_metric("EC2.Size", fleet_size if fleet_size else None, dimensions=dimensions)
                cw.set_metric("EC2.ExcludedInstances", 
                        excluded_count if fleet_size else None, dimensions=dimensions)
                cw.set_metric("EC2.RunningInstances", 
                        len(running_instances) if fleet_size else None, dimensions=dimensions)
                cw.set_metric("EC2.DrainingInstances", 
                        len(draining_instances) if fleet_size else None, dimensions=dimensions)
                send_metric    = (expected_state == "running") and fleet_size
                cw.set_metric("EC2.MinInstanceCount", 
                        min_instance_count if send_metric else None, dimensions=dimensions)
                cw.set_metric("EC2.DesiredInstanceCount", 
                        desired_instance_count if send_metric else None, dimensions=dimensions)
                cw.set_metric("EC2.NbOfInstanceInUnuseableState", 
                        len(subfleet_faulty_instance_ids_w_excluded) if fleet_size else None, dimensions=dimensions)
                cw.set_metric("EC2.NbOfInstanceInInitialState", 
                        len(initial_instances) if fleet_size else None, dimensions=dimensions)
                cw.set_metric("SSM.MaintenanceWindow", 
                        self.ssm.is_maintenance_time(fleet=subfleet) if self.ssm.is_feature_enabled("maintenance_window") else None, 
                        dimensions=dimensions)
            else:
                cw.set_metric("EC2.Size", None, dimensions=dimensions)
                cw.set_metric("EC2.ExcludedInstances", None, dimensions=dimensions)
                cw.set_metric("EC2.RunningInstances", None, dimensions=dimensions)
                cw.set_metric("EC2.DrainingInstances", None, dimensions=dimensions)
                cw.set_metric("EC2.MinInstanceCount", None, dimensions=dimensions)
                cw.set_metric("EC2.DesiredInstanceCount", None, dimensions=dimensions)
                cw.set_metric("EC2.NbOfInstanceInUnuseableState", None, dimensions=dimensions)
                cw.set_metric("EC2.NbOfInstanceInInitialState", None, dimensions=dimensions)
                cw.set_metric("SSM.MaintenanceWindow", None, dimensions=dimensions)

    def generate_subfleet_dashboard(self):
        """ Create / Destroy CloudWatch dashboards.
        """
        now                = self.context["now"]
        dashboard          = { "widgets": [] }
        subfleets          = sorted(self.ec2.get_subfleet_names())
        fleet_with_details = []
        for i in range(0, len(subfleets)):
            subfleet_name = subfleets[i]
            if not get_subfleet_key(f"ec2.schedule.metrics.enable", subfleet_name, cls=int):
                continue
            fleet_with_details.append(subfleet_name)
            widget = {
                    "type": "metric",
                    "x": 0 if (i+1) % 2 else 12,
                    "y": 1 + int(i / 2) * 6,
                    "width": 12,
                    "height": 6,
                    "properties": {
                        "view": "timeSeries",
                        "stacked": False,
                        "metrics": [
                            [ "CloneSquad", "EC2.Size", "GroupName", self.context["GroupName"], "SubfleetName", subfleet_name ],
                            [ ".", "EC2.ExcludedInstances", ".", ".", ".", "." ],
                            [ ".", "EC2.MinInstanceCount", ".", ".", ".", "." ],
                            [ ".", "EC2.DesiredInstanceCount", ".", ".", ".", "." ],
                            [ ".", "EC2.NbOfCPUCreditingInstances", ".", ".", ".", "." ],
                            [ ".", "EC2.DrainingInstances", ".", ".", ".", "." ],
                            [ ".", "EC2.RunningInstances", ".", ".", ".", "." ],
                            [ ".", "EC2.NbOfInstanceInUnuseableState", ".", ".", ".", "." ],
                            [ ".", "EC2.NbOfInstanceInInitialState", ".", ".", ".", "." ],
                            [ ".", "SSM.MaintenanceWindow", ".", ".", ".", "." ]
                        ],
                        "region": self.context["AWS_DEFAULT_REGION"],
                        "title": subfleet_name,
                        "period": 60,
                        "stat": "Average"
                    }
                }
            dashboard["widgets"].append(widget)

        use_dashboard    =  Cfg.get_int("cloudwatch.subfleet.use_dashboard") if len(dashboard["widgets"]) != 0 else False
        fingerprint      = misc.sha256(f"{use_dashboard},%s,%s,%s" % 
                (Dbg.pprint(fleet_with_details), Dbg.pprint(dashboard["widgets"]),
                now.minute / 15)) # Make the fingerprint change every 15 minutes
        last_fingerprint = self.ec2.get_state("cloudwatch.subfleet.last_fingerprint")
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
                dashboard["widgets"].append({
                    "type": "text",
                    "x": 0,
                    "y": 0,
                    "width": 24,
                    "height": 1,
                    "properties": {
                       "markdown": "\n### This is an automatically generated dashboard. **DO NOT EDIT!**\n"
                        }
                    })
                log.log(log.NOTICE, "Configuring Subfleet CloudWatch dashboard...")
                response = client.put_dashboard(
                        DashboardName="CS-%s-Subfleets" % self.context["GroupName"],
                        DashboardBody=Dbg.pprint(dashboard)
                    )
        self.set_state("cloudwatch.subfleet.last_fingerprint", fingerprint)


    ###############################################
    #### SCALE DESIRED & SCALE BOUNCE ALGOS #######
    ###############################################

    @xray_recorder.capture()
    def manage_excluded_instances(self):
        """ This function is responsible to send events linked excluded/unexcluded instances and start needed
            instances in the Main fleet when in auto-scaling mode.
        """
        current_excluded    = [i["InstanceId"] for i in self.all_instances if self.ec2.is_instance_excluded(i)]
        known_excluded      = self.ec2.get_state_json("ec2.schedule.instance.known_excluded_instances", default=[])

        if Cfg.get("ec2.schedule.desired_instance_count") == "-1":
            # When autoscaling mode is acticated, we compensate excluded instances by fresh instances
            main_excluded = [i["InstanceId"] for i in self.ec2.get_instances(main_fleet_only=True) if self.ec2.is_instance_excluded(i)]
            new_main_excl = [i for i in main_excluded if i not in known_excluded]
            if len(new_main_excl):
                log.info(f"Starting %s main fleet instance(s) due to new detected excluded instances: {new_main_excl}...")
                self.instance_action(self.useable_instance_count + len(new_main_excl), "manage_excluded_instances") 

        new_excluded        = []
        for ex_id in current_excluded:
            if ex_id not in known_excluded:
                new_excluded.append(ex_id)
        new_unexcluded      = []
        for ex_id in known_excluded:
            if ex_id not in current_excluded:
                new_unexcluded.append(ex_id)

        new_known_excluded  = [i for i in known_excluded if i not in new_unexcluded]
        new_known_excluded.extend(new_excluded)
        if known_excluded != new_known_excluded:
            log.log(log.NOTICE, f"Notify excluded_instance_transitions: {new_known_excluded},"
                " {new_excluded}, {new_known_excluded}")
            R(None, self.excluded_instance_transitions, ExcludedInstanceIds=new_known_excluded, 
                    NewExcludedInstanceIds=new_excluded, NewUnexcludedInstanceIds=new_unexcluded)
        self.ec2.set_state_json("ec2.schedule.instance.known_excluded_instances", new_known_excluded, TTL=self.state_ttl)

    def excluded_instance_transitions(self, ExcludedInstanceIds=None, NewExcludedInstanceIds=None, NewUnexcludedInstanceIds=None):
        return {}


    @xray_recorder.capture()
    def scale_desired(self):
        """ Module entrypoint to take scale decisions based on 'ec2.schedule.desired_instance_count' criteria
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
        """ Return 'True' if the bouncing algorithm is allowed to stop instances now.
        """
        scale_down_disabled = Cfg.get_int("ec2.schedule.scalein.disable") != 0
        return scale_down_disabled or self.instance_scale_score < Cfg.get_float("ec2.schedule.scalein.threshold_ratio")

    @xray_recorder.capture()
    def scale_bounce(self):
        """ IF configured, bounce old out-of-date instances by spawning new ones.
        """
        if self.scaling_state_changed:
            return

        now = self.context["now"]
        bounce_delay_delta             = timedelta(seconds=Cfg.get_duration_secs("ec2.schedule.bounce_delay"))
        bounce_instance_cooldown_delta = timedelta(seconds=Cfg.get_duration_secs("ec2.schedule.bounce_instance_cooldown"))

        instances = self.pending_running_instances_wo_excluded 
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

                    subfleet = self.ec2.get_subfleet_name_for_instance(i)
                    if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=subfleet):
                        log.info(f"Bouncing actions disabled during '{subfleet}' subfleet SSM Maintenance Window: "
                            f"Should have placed in 'draining' state instance {instance_id}...")
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

                subfleet = self.ec2.get_subfleet_name_for_instance(i)
                if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=subfleet):
                    log.info(f"Bouncing actions disabled during '{subfleet}' subfleet SSM Maintenance Window: "
                        f"Should have placed in 'bounced' state instance {instance_id}..")
                    continue

                to_bounce_instance_ids.append(instance_id)
                # Mark instance as 'bounced' with a not too big TTL. If bouncing
                self.ec2.set_scaling_state(instance_id, "bounced")
                                                                                                      

        if len(to_bounce_instance_ids) == 0:
            return

        log.info("Bouncing of instances %s in progress..." % to_bounce_instance_ids)
        self.instance_action(self.useable_instance_count + len(to_bounce_instance_ids), "scale_bounce")

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
        new_unuseable_ids     = []
        for i in instances_with_issues:
            last_seen_date = self.ec2.get_state_date("ec2.schedule.bounce.instance_with_issues.%s" % i, TTL=grace_period * 1.2)
            if self.ec2.get_scaling_state(i) == "draining":
                continue
            if last_seen_date is None:
                self.ec2.set_state("ec2.schedule.bounce.instance_with_issues.%s" % i, now, TTL=grace_period * 1.2)
                new_unuseable_ids.append(i)
                continue
            if (now - last_seen_date).total_seconds() > grace_period:
                subfleet = self.ec2.get_subfleet_name_for_instance(i)
                if self.ssm.is_feature_enabled("maintenance_window") and self.ssm.is_maintenance_time(fleet=subfleet):
                    fleet_name = "Main" if subfleet is None else f"Subfleet.{subfleet}" 
                    log.info(f"Bouncing actions disabled during '{fleet_name}' fleet SSM Maintenance Window: "
                        f"Should have placed in 'draining' state instance {i}...")
                    continue
                self.ec2.set_scaling_state(i, "draining")
                log.info("Bounced instance '%s' with issues as grace period expired!" % i)

        # Garbage collect outdated keys
        prefix = "ec2.schedule.bounce.instance_with_issues."
        for k in self.o_state.get_keys(prefix=prefix):
            instance_id = k[len(prefix):]
            if instance_id not in instances_with_issues:
                self.ec2.set_state(f"ec2.schedule.bounce.instance_with_issues.{instance_id}", "")

        # Send user notification
        if len(new_unuseable_ids):
            log.log(log.NOTICE, f"Instances seen 'unuseable' for the first time: {new_unuseable_ids}")
            # Notify the user that new instance just became unuseable
            R(None, self.new_instances_marked_as_unuseable, InstanceIds=new_unuseable_ids)

    def new_instances_marked_as_unuseable(self, InstanceIds=None):
        return {}
        

    
    ###############################################
    #### CORE SCALEUP/DOWN ALGORITHM ##############
    ###############################################

    def get_lighthouse_instance_ids(self, instances):
        """ Retrieve the list of LightHouse instances.

        LightHouse instances are designated based on their instance type. LightHouse types are 
        defined in the vertical scaling configuration (defined in 'ec2.schedule.verticalscale.instance_type_distribution').

        (TODO: Remove the code provision that allow definition of LightHouse instance with Tag that is deprecated.)

        :return A list of Instance Ids
        """
        # Excluded subfleet instances
        subfleet_instances    = self.ec2.filter_instance_list_by_tag(instances, "clonesquad:subfleet-name")
        subfleet_instance_ids = [i["InstanceId"] for i in subfleet_instances]
        instances             = [i for i in instances if i["InstanceId"] not in subfleet_instance_ids]

        # Collect instances that are marked as LightHouse through a Tag
        ins = self.ec2.filter_instance_list_by_tag(instances, "clonesquad:lighthouse", ["True","true"])
        ids = [i["InstanceId"] for i in ins]

        # Collect instances that are declared as LightHouse through Vertical scaling
        cfg = Cfg.get_list_of_dict("ec2.schedule.verticalscale.instance_type_distribution")
        lighthouse_instance_types = [t["_"] for t in list(filter(lambda c: "lighthouse" in c and c["lighthouse"], cfg))]

        for t in lighthouse_instance_types:
            ins = list(filter(lambda i: re.match(t, i["InstanceType"]), instances))
            for i in [i["InstanceId"] for i in ins]: 
                if i not in ids: ids.append(i)
        return ids

    def are_lighthouse_instance_disabled(self):
        """ Return True if LightHouse instance support is disable by 'ec2.schedule.verticalscale.lighthouse_disable'.
        """
        all_lh_ids     = self.lighthouse_instances_wo_excluded_ids 
        return (Cfg.get_int("ec2.schedule.verticalscale.lighthouse_disable") or
                  self.get_min_instance_count() > len(all_lh_ids))

    def are_all_non_lh_instances_started(self):
        """ Return 'True' if all startable non-LightHouse instances are already started.
        """
        all_instances              = self.instances_wo_excluded_error_spotexcluded
        lh_ids                     = self.lighthouse_instances_wo_excluded_ids
        useable_instances          = self.useable_instances
        non_lighthouse_instances   = list(filter(lambda i: i["InstanceId"] not in lh_ids, useable_instances))
        all_non_lighthouse_instances       = list(filter(lambda i: i["InstanceId"] not in lh_ids, all_instances))
        all_non_lighthouse_instances_count = len(all_non_lighthouse_instances)
        return all_non_lighthouse_instances_count == len(non_lighthouse_instances)

    def shelve_instance_dispatch(self, expected_count):
        """ This method returns the expected amount of LightHouse and non-LightHouse instances depending of overall expected
        serving instances in the Main fleet.

        This method is a critical one for the scalin/scaleout algorithms as they rely on it to know when LightHouse needs to
        be stopped and started. The general concept is that when the number of expected serving instances in the fleet is
        approaching 'min_instance_count', this method computes the expected number of running LH instances.  

        Ex: Let assume we have 'min_instance_count' set to value '2'. What happen when expected instance count vary?
            +----------------+--------------------------------+
            | expected_count | nb_of_recommended_LH_instances |
            +----------------+--------------------------------+
            | 2              | 2                              |
            | 3              | 1                              |
            | 4 and more...  | 0                              |
            +----------------+--------------------------------+

        :return (expected_number_of_running_LH_instances, expected_number_of_running-non_LH_instances)
        """
        desired_instance_count= self.desired_instance_count()
        min_instance_count    = self.get_min_instance_count()
        useable_instance_count= self.useable_instance_count
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

    def verticalscaling_sort_instances(self, directive, instances, reverse=False):
        """ Sort the supplied instance list according to vertical scaling directive string.

        This method is used both in Main fleet and subfleets scaling algorithms.

        :return A dict of sorted instance lists 
            - One list for LightHouse instances, 
            - One list for sorted non-LightHouse instance,
            - One list for instances that do not match the vertical scaling policy.
        """
        def _match_spot(i, c, spot_implicit=None):
            cc = c.copy()
            if spot_implicit is not None and "spot" not in cc:
                cc["spot"] = spot_implicit
            if "spot" in cc:
                is_spot = self.ec2.is_spot_instance(i)
                if cc["spot"] and not is_spot: return False
                if not cc["spot"] and is_spot: return False
            return True

        directive_items = misc.parse_line_as_list_of_dict(directive, default=[])
        if reverse:
            instances = instances.copy()
            instances.reverse()
        lh_ids          = self.get_lighthouse_instance_ids(instances)
        r               = { "directives" : directive_items }

        r["lh_instances"]     = list(filter(lambda i: i["InstanceId"] in lh_ids, instances))

        # Sort non-LH instances
        insts          = []
        non_lh_ids     = []
        for d in directive_items:
            try:
                i_s        = list(filter(lambda i: re.match(d["_"], i["InstanceType"]) and i["InstanceId"] not in lh_ids and i["InstanceId"] not in non_lh_ids and _match_spot(i, d), instances))
            except Exception as e:
                log.error(f"Format error with Regex '%s' inside vertical scaling directive '{directive}'! Please express a valid Regex to match instance type!" %
                            (d["_"]))
                continue
            for i in i_s:
                insts.append(i)
                non_lh_ids.append(i["InstanceId"])
        r["non_lh_instances"] = insts
        if reverse:
            r["non_lh_instances"].reverse()

        # Identify all instances that are not LH or not part of the vertical policy
        valid_non_lh_instance_ids                 = [ i["InstanceId"] for i in insts]
        r["non_lh_instances_not_matching_policy"] = list(filter(lambda i: i["InstanceId"] not in valid_non_lh_instance_ids and i["InstanceId"] not in lh_ids, instances))

        return r

    def scaleup_sort_instances(self, candidates, expected_count, caller):
        """ Sort candidate instances to be started on a scaleout event in the Main fleet.

        This method has the major responsability to promote LightHouse instances when needed.
        When no LightHouse configuration exits, this method is basically a pass-through.

        :return A sorted list of instances ordered from the highest priority instance to start to the lower one
        """

        # Special behavior when min_instance_count and desired_instance_count are both set to the string '100%':
        #   This is the official way to request that all instances --including the LightHouse ones-- needs to be started.
        if Cfg.get("ec2.schedule.min_instance_count") == "100%" and Cfg.get("ec2.schedule.desired_instance_count") == "100%":
            log.info("'ec2.schedule.min_instance_count' and 'ec2.schedule.desired_instance_count' are both set to '100%': Start all instances including LightHouse ones!")
            return candidates

        # Sort instances according to vertical policy
        vertical_sorted_instances  = self.verticalscaling_sort_instances(Cfg.get("ec2.schedule.verticalscale.instance_type_distribution"), candidates)

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
        instances.extend(vertical_sorted_instances["lh_instances"][:lighthouse_need])

        if not lighthouse_only:
            instances.extend(vertical_sorted_instances["non_lh_instances"])

            # All instances with unknown instance types are low priority
            instances_with_unmatching_instance_types = vertical_sorted_instances["non_lh_instances_not_matching_policy"]
            if len(vertical_sorted_instances["directives"]) and len(instances_with_unmatching_instance_types):
                log.warning(f"Instances %s do not match vertical scaling policy defined by `ec2.schedule.verticalscale.instance_type_distribution`. "
                        "These instances will be considered as low priority by default. Please adjust either the vertical policy or the "
                        "instance type of these instances to allow normal vertical scaler operations." % 
                            ([(i["InstanceId"], i["InstanceType"]) for i in instances_with_unmatching_instance_types]))
            instances.extend(instances_with_unmatching_instance_types)

            if not lighthouse_disabled:
                # We consider to scaleout with Lighthouse instances only if there is no running non-lighthouse instance
                #   or if all non-lighthouse instances are already started
                if (len(non_lighthouse_instances) == 0 or 
                    (self.desired_instance_count() != -1 and self.are_all_non_lh_instances_started())
                   ):
                    instances.extend(vertical_sorted_instances["lh_instances"][lighthouse_need:])

        return instances

    def scaledown_sort_instances(self, candidates, expected_count, caller):
        """ Sort candidate instances to be stopped on a scalein event in the Main fleet.

        This method has the major responsability to promote LightHouse instances when needed.
        When no LightHouse configuration exits, this method is basically a pass-through.

        :return A sorted list of instances ordered from the highest priority instance to stop to the lower one
        """
        # Sort instances according to vertical policy (non LH instances are sorted reversed; starting from the end of the policy
        vertical_sorted_instances  = self.verticalscaling_sort_instances(Cfg.get("ec2.schedule.verticalscale.instance_type_distribution"), 
                candidates, reverse=True)

        instances = []
        # Put instance with incorrect instance types first to make them drained first
        instances_with_unmatching_instance_types = vertical_sorted_instances["non_lh_instances_not_matching_policy"]
        if len(vertical_sorted_instances["directives"]) and len(instances_with_unmatching_instance_types):
            log.warning(f"Instances %s do not match vertical scaling policy defined by `ec2.schedule.verticalscale.instance_type_distribution`. "
                    "These instances will be considered as low priority by default. Please adjust either the vertical policy or the "
                    "instance type of these instances to allow normal vertical scaler operations." % 
                        ([(i["InstanceId"], i["InstanceType"]) for i in instances_with_unmatching_instance_types]))
        instances.extend(instances_with_unmatching_instance_types)


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
        instances.extend(vertical_sorted_instances["non_lh_instances"])

        # By default, all lighthouse instances are low priority to stop (lighthouse instances are only stopped by
        #   'shelve_extra_lighthouse_instances' process and not the 'scalin' one
        instances.extend(lighthouse_instances[lighthouse_need:])

        return instances

    @xray_recorder.capture()
    def shelve_extra_lighthouse_instances(self):
        """ Manage the lifecycle of LightHouse instances.

        This method makes sure that LightHouse instances are started or stopped when needed.
        
        If no LightHouse configuration is defined in the vertical scaling configuration, it does basically nothing.
        """
        if self.scaling_state_changed:
            return

        # If the user is forcing the fleet to run at max capacity, disable all fancy algorithms
        if Cfg.get("ec2.schedule.desired_instance_count") == "100%":
            return

        now = self.context["now"]

        min_instance_count                       = self.get_min_instance_count()
        all_instances                            = self.instances_wo_excluded 
        all_useable_plus_special_state_instances = self.useable_instances_wo_excluded_draining 
        serving_instances                        = self.serving_instances   
        lh_ids                                   = self.lighthouse_instances_wo_excluded_ids  
        running_lh_ids                           = self.get_lighthouse_instance_ids(all_useable_plus_special_state_instances)
        non_lighthouse_instances                 = self.serving_non_lighthouse_instance_ids  
        non_lighthouse_instances_initializing    = self.serving_non_lighthouse_instance_ids_initializing 
        serving_non_lighthouse_instance_count    = len(non_lighthouse_instances) - len(non_lighthouse_instances_initializing)
        useable_instance_count                   = self.useable_instance_count
        desired_instance_count                   = self.desired_instance_count()

        lighthouse_instance_excess = 0

        initializing_lh_instances_ids = self.get_lighthouse_instance_ids(self.initializing_instances) 
        if not self.are_lighthouse_instance_disabled():
            if len(initializing_lh_instances_ids):
                # A a general strategy, we do not take scaling decisions when there is at least one instance in 'initializing' state.
                log.debug("Some LightHouse instances (%s) are still initializing... Postpone shelve processing..." % 
                        initializing_lh_instances_ids)
                return
            if serving_non_lighthouse_instance_count < min_instance_count and len(non_lighthouse_instances_initializing):
                # A a general strategy, we do not take scaling decisions when there is not enough serving non-LH instances.
                #   Shuttingdown LightHouse instances while not enough non-LightHouse instances were ready, could lead to
                #   fleet instability so we prefer to postpone the processing.
                log.debug("Not enough serving non-LH instances while some are initializing (%s)... Postpone shelve processing..." % 
                        non_lighthouse_instances_initializing)
                return

        max_lh_instances = len(lh_ids)
        expected_count   = desired_instance_count if desired_instance_count != -1 else useable_instance_count
        expected_count   = max(expected_count, min_instance_count)
        amount_of_lh, amount_of_non_lh = self.shelve_instance_dispatch(expected_count)

        # Take into account unhealthy/unavailable LH instances
        instances_with_issues_ids = self.instances_with_issues 
        instances_with_issues     = [ i for i in self.instances_wo_excluded if i["InstanceId"] in instances_with_issues_ids] 
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

        extra_instance_count = self.useable_instance_count - min_instance_count
        if extra_instance_count + delta_lh < 0:
            # We do not have enough spare instances available to reduce the fleet size without
            #   falling under min_instance_count.
            #   As consequence, we prefer to launch new fresh instances to have the opportunity
            #   later to discard the ones that should go.
            delta_lh = abs(delta_lh)

        if delta_lh != 0:
            if desired_instance_count != -1:
                # 'desired_instance_count' != -1 so the autoScaler is disabled. In such mode, we only add new instances as
                #   'scale_desired' algorithm will shutdown supernumerary instances following the vertical scaling policy.
                #
                #  Ex: If this algorithm wants to stop 2 existing LH instances, it will ask to start 2 new instances and the 
                #  vertical scaler will launch 2 non-LH instances. Just after, 'scale_desired' algorithm will select the LH 
                # instances as priority for shutdown.
                if self.get_useable_instance_count(exclude_initializing_instances=True) < desired_instance_count: 
                    delta_lh = abs(delta_lh)
            else:
                # 'desired_instance_count' == -1 so the AutoScaler is enabled. The AutoScaler will do most of the job dealing
                #   with LH instances. This algorithm is only there to anticipate the need to stop LH instances during an active
                #   scaleout sequence. Without this code, LH instances would be stopped by the Autocaler only when starting a
                #   scalein sequence. 
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


    ##########################################################
    #### CORE MAIN FLEET SCALEIN/OUT ALGORITHM ###############
    ##########################################################

    def get_scale_start_date(self, direction):
        return self.ec2.get_state_date("ec2.schedule.%s.start_date" % direction)

    def is_scale_transition_too_early(self, to_direction):
        """ Return 0 if it is not yet the time to consider a scaling sequence in specified direction.

        To avoid any fast flip/flop between scalein/scaleout sequences, there are cooldown delays specific to
        each direction (defined by 'ec2.schedule.to_scalein_state.cooldown_delay' and 'ec2.schedule.to_scaleout_state.cooldown_delay')

        :param direction: ["scalein", "scaleout"]
        :return 0 if we can start a sequence in the specified direction. Return the number of seconds to wait.
        """
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
        """ Method responsible to compute the scaling score used to decide if we need to scalein or scaleout in the Main fleet.

        This method is critical one as it contains the logic to generate the scaling score that will be used by the autoscaler
        especially to decide to scaleout or scalein.

        It relies on CloudWatch alarms:
            - It watchs for alarm in ALARM state.
            - It watchs for metrics defined in Alarms to get numeric details and so enable a smooth score calculation.

        Few critical tasks are implemented in this method:
            - Calculating a score per instance based on a read metric value and a BaseLine threshold and Alamr Threshold
            - Give weight to metrics based on their "freshness" (to reduce costs, all metrics are not polled at each
                Main function execution.

        Please read for more detail about the algorithm here: 
            https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/ALARMS_REFERENCE.md

        :param assesment:       Contains a dict of instances that has at least one CloudWatch alarm in ALARM state.
        :param default_points:  When no alarm points is defined for a specific alarm, it is the default value (usually, 1000)
        :return                 A number of that represents the scaling score.
        """
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
        """ This method takes autoscaling decisions starting or stopping instances in the Main fleet.
        
        TODO: Almost all this code could be removed as storing alarms is no more an effective way to manage them. It is now redundant with
            take_scale_decision() and get_guilties_sum_points() that read and react directly by polling all the alarms.
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
        """ Return the number of instances to start or stop in the Main fleet.

        This method implements a time-based and 'boost_rate' based calculation about when to start or stop 
        instances. 
            * The time-based part is derived from config keys 'ec2.schedule.{direction}.period' and 'ec2.schedule.{direction}.rate'
                Ex: if ec2.schedule.{direction}.period is set to 10 minutes and ec2.schedule.{direction}.rate set to 4, it means 4 instances per 10 minutes.
            * The 'boost_rate' acts as a multiplier of the previous rate.
                Ex: If boost_rate = 2.0, in the context of the previous example, it means that the effective rate will be 8 instances per 10 minutes.

        :param direction: The direction currently assessed (among ["scalein", "scaleout"])
        :param boost_rate: A float representing the "urgency" to go in the specified direction
        :return An integer (positive if instances needs to be started; negative otherwise)
        """
        now = self.context["now"]

        instance_upfront_count = 0
        last_scale_start_date = self.get_scale_start_date(direction)
        new_scale_sequence    = False
        if last_scale_start_date is None:
            # Remember when we started to scale 
            last_scale_start_date  = now
            last_event_date        = last_scale_start_date
            new_scale_sequence     = True
            # Do we have to (over)react because of new sequence?
            instance_upfront_count = Cfg.get_int("ec2.schedule.%s.instance_upfront_count" % direction)
        else:
            last_event_date        = self.ec2.get_state_date("ec2.schedule.%s.last_action_date" % direction, default=last_scale_start_date)
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

        # Report some printable statistics
        text.append("\n".join(["[INFO] Scaling data for direction '%s' :" % direction,
                "   Now: %s" % now,
                "   Scale start date: %s" % str(last_scale_start_date),
                "   Latest scale event date: %s" % str(last_event_date),
                "   Seconds since scale start: %d" % seconds_since_scale_start.total_seconds(),
                "   Seconds since latest scale event date: %d" % seconds_since_latest_scale_event.total_seconds(),
                "   Rate: %d instance(s) per period" % rate,
                "   Nominal Period: %d seconds" % period,
                "   Boost rate: %f" % boost_rate,
                "   Boosted effective period: %d seconds" % (period / boost_rate),
                "   Effective rate over %ds period: %.1f instance per period (%.1f instance(s) per minute)" % (period, ratio * period, ratio * 60),
                "   Computed raw instance count: %.2f" % raw_instance_count
                ]))

        return delta_count

    @xray_recorder.capture()
    def take_scale_decision(self, items, assessment):
        """ This method use the scaling score to invoke either the scalein or scaleout algorithm to manage the Main fleet autoscaling

        :param items:       List of CloudWatch alarms
        :param assessment:  List of CloudWatch alarms in ALARM state
        """
        now = self.context["now"]

        # Step 1) Update the scaling score
        
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

        # Step 2.a) Check if we are allowed to scaleout now
        if not scale_up_disabled and self.instance_scale_score >= 1.0:
            self.take_scale_decision_scaleout()
            return

        # Remember that we are not in an scaleout condition here
        self.set_state("ec2.schedule.scaleout.start_date", "")
        self.set_state("ec2.schedule.scaleout.last_action_date", "")

        # Step 2.b) Check if we are allowed to scalein now
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
        now          = self.context["now"]
        time_to_wait = self.is_scale_transition_too_early("scaleout")
        if time_to_wait > 0:
            log.log(log.NOTICE, "Transition period from scalein to scaleout (%d seconds still to go...)" % time_to_wait)
            return

        text              = []
        instance_to_start = self.get_scale_instance_count("scaleout", self.instance_scale_score, text)
        if instance_to_start == 0: 
            self.would_like_to_scaleout = True
            return
        log.debug(text[0])
        useable_instances_count = self.useable_instance_count
        desired_instance_count  = useable_instances_count + instance_to_start

        log.log(log.NOTICE, "Need to start up to '%d' more instances (Total expected=%d)" % (instance_to_start, desired_instance_count))
        self.instance_action(desired_instance_count, "scaleout")
        self.set_state("ec2.schedule.scaleout.last_action_date", now)

    @xray_recorder.capture()
    def take_scale_decision_scalein(self):
        now                         = self.context["now"]
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
        """ Part of get_prerequisites() processing, this method computes instance lists linked to Spot instances.

        The method builds list of Spot interrupted and rebalance recommended used by scaling algorithms.
        """
        # Collect all Spot instances events
        self.spot_rebalance_recommanded     = self.ec2.filter_spot_instances(self.all_instances, EventType="+rebalance_recommended")
        self.spot_rebalance_recommanded_ids = [ i["InstanceId"] for i in self.spot_rebalance_recommanded ]
        self.spot_interrupted               = self.ec2.filter_spot_instances(self.all_instances, EventType="+interrupted")
        self.spot_interrupted_ids           = [ i["InstanceId"] for i in self.spot_interrupted ]
        self.spot_excluded_instance_ids.extend(self.spot_rebalance_recommanded_ids)
        self.spot_excluded_instance_ids.extend(self.spot_interrupted_ids)

        # Uncomment this to enable blacklisting of all instances sharing the same type and AZ than the ones that received a Spot message.
        #    TODO: Clarify if this strategy could render useful in real life and propose a toggle to activate it.
        #
        #for event_type in [self.spot_rebalance_recommanded, self.spot_interrupted]:
        #    for i in event_type:
        #        instance_type  = i["InstanceType"]
        #        instance_az    = i["Placement"]["AvailabilityZone"]
        #        if instance_type not in self.excluded_spot_instance_types:
        #            self.excluded_spot_instance_types.append({
        #                "AvailabilityZone" : instance_az,
        #                "InstanceType"     : instance_type
        #                })
        #if len(self.excluded_spot_instance_types):
        #    log.warning("Some instance types (%s) are temporarily blacklisted for Spot use as marked as interrupted or close to interruption!" %
        #            self.excluded_spot_instance_types)
        # Gather all instance ids of interrupted Spot instances
        #for i in self.ec2.filter_spot_instances(self.pending_running_instances, filter_in_instance_types=self.excluded_spot_instance_types, match_only_spot=True):
        #    if i["InstanceId"] not in self.spot_excluded_instance_ids:
        #        self.spot_excluded_instance_ids.append(i["InstanceId"])

        self.instances_wo_spotexcluded                = self.ec2.filter_spot_instances(self.all_instances, 
                filter_out_instance_types=self.excluded_spot_instance_types)
        self.instances_wo_excluded_error_spotexcluded = self.ec2.get_instances(self.instances_wo_spotexcluded, ScalingState="-error,excluded") 


    def manage_spot_events(self):
        """ Manage the life cycle of Spot instances (both in Main fleet and subfleets).

        This method marks Spot instance in 'interrupted' state as 'draining'. It also reacts to Spot events but launching replacement
        instances immediatly after EC2 Spot message receipt.
        """
        if len(self.spot_rebalance_recommanded_ids):
            log.info("EC2 Spot instances with 'rebalance_recommended' status: %s" % self.spot_rebalance_recommanded_ids)
        if len(self.spot_interrupted_ids):
            log.info("EC2 Spot instances with 'interrupted' status: %s" % self.spot_interrupted_ids)

        # Mark all Spot interrupted as 'draining'
        for i in self.spot_interrupted:
            instance_id    = i["InstanceId"]
            if i["State"]["Name"] == "running":
                self.ec2.set_scaling_state(instance_id, "draining")
                log.info(f"Set 'draining' state for Spot interrupted instance '{instance_id}'.")

        # Launch new instances when some Spot instances have been just 'recommended' or 'interrupted'

        # Load state of what we know about former EC2 Spot processed messages
        known_spot_advisories    = self.ec2.get_state_json("ec2.schedule.instance.spot.known_spot_advisories", default=None)
        if known_spot_advisories is None:
            known_spot_advisories = {}

        subfleet_deltas          = defaultdict(int)
        instance_count_to_launch = 0
        for i in self.spot_rebalance_recommanded:
            instance_id = i["InstanceId"]
            if instance_id not in known_spot_advisories and instance_id not in self.spot_interrupted_ids:
                log.info(f"Instance '{instance_id}' just got 'Spot rebalance recommended' message. Launch immediatly "
                    "a new instance to anticipate a possible interruption!")
                known_spot_advisories[instance_id] = "recommended"
                if not self.ec2.is_subfleet_instance(instance_id):
                    instance_count_to_launch += 1
                else:
                    subfleet_deltas[self.ec2.get_subfleet_name_for_instance(i)] += 1
        for i in self.spot_interrupted:
            instance_id = i["InstanceId"]
            if instance_id not in known_spot_advisories or known_spot_advisories[instance_id] != "interrupted":
                log.info(f"Instance '{instance_id}' just got 'Spot interrupted' message. Launch immediatly a new instance!")
                known_spot_advisories[instance_id] = "interrupted"
                if not self.ec2.is_subfleet_instance(instance_id):
                    instance_count_to_launch += 1
                else:
                    subfleet_deltas[self.ec2.get_subfleet_name_for_instance(i)] += 1

        if instance_count_to_launch:
            # Launch needed instances in the Main fleet
            self.instance_action(self.useable_instance_count + instance_count_to_launch, "manage_spot_events")

        for subfleet in subfleet_deltas:
            # Launch needed instances in each subfleet
            log.info(f"Due to Spot instance status change, %d instances have to be started in subfleet '{subfleet}'." % subfleet_deltas[subfleet])
            self.subfleet_action(subfleet, subfleet_deltas[subfleet])

        # Garbage collect old instance notifications
        for i in list(known_spot_advisories.keys()):
            if i not in self.spot_rebalance_recommanded_ids and i not in self.spot_interrupted_ids:
                del known_spot_advisories[i]

        # Persist that we managed Spot events
        self.ec2.set_state_json("ec2.schedule.instance.spot.known_spot_advisories", known_spot_advisories, TTL=self.state_ttl)

