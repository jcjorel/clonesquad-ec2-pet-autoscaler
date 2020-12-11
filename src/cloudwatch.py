import os
import re
import json
import yaml
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import base64
import boto3

import pdb
import misc
import config as Cfg
import debug as Dbg
from notify import record_call as R

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class CloudWatch:
    @xray_recorder.capture(name="Cloudwatch.__init__")
    def __init__(self, context, ec2):
        self.context = context                   
        self.ec2 = ec2
        self.alarms = None
        self.metrics = []
        Cfg.register({
                    "cloudwatch.describe_alarms.max_results" : "50",
                    "cloudwatch.default_ttl": "days=1",
                    "cloudwatch.alarms.max_per_instance" : "6",
                    "cloudwatch.alarms.min_instance_age" : "minutes=3",
                    "cloudwatch.configure.max_alarms_deleted_batch_size" : "5",
                    "cloudwatch.metrics.namespace": "CloneSquad",
                    "cloudwatch.metrics.subnamespace": "",
                    "cloudwatch.metrics.excluded,Stable": {
                        "DefaultValue": "",
                        "Format"      : "StringList",
                        "Description" : """List of metric pattern names to not send to Cloudwatch

This configuration key is used to do Cost optimization by filtering which CloneSquad Metrics are sent to Cloudwatch.
It support regex patterns.

> Ex: StaticFleet.*;NbOfBouncedInstances

                        """
                    },
                    "cloudwatch.metrics.data_period": "minutes=2",
                    "cloudwatch.metrics.max_update_per_batch": "20",
                    "cloudwatch.metrics.cache.max_retention_period": "minutes=15",
                    "cloudwatch.metrics.instance_minimum_age_for_cpu_credit_polling": "minutes=10",
                    "cloudwatch.metrics.minimum_polled_alarms_per_run": "1",
                    "cloudwatch.metrics.time_for_full_metric_refresh,Stable": {
                        "DefaultValue": "minutes=1,seconds=30",
                        "Format": "Duration",
                        "Description": """The total period for a complete refresh of EC2 Instance metrics

This parameter is a way to reduce Cloudwatch cost induced by GetMetricData API calls. It defines indirectly how many alarm metrics
will be polled in a single Main Lambda execution. A dedicated algorithm is used to extrapolate missing data based
on previous GetMetricData API calls.

Reducing this value increase the accuracy of the scaling criteria and so, the reactivity of CloneSquad to a sudden burst of activity load but at the
expense of Cloudwatch.GetMetricData API cost.

This parameter does not influence the polling of user supplied alarms that are always polled at each run.
                        """
                    },
                    "cloudwatch.dashboard.use_default,Stable": {
                        "DefaultValue" : 1,
                        "Format"       : "Bool",
                        "Description"  : """Enable or disable the Cloudwatch dashboard for CloneSquad.

The dashboard is enabled by default.
                        """
                    },
                    "cloudwatch.dashboard.update_interval": "minutes=30",
                    "cloudwatch.dashboard.snapshot_width": 1000,
                    "cloudwatch.dashboard.snapshot_height": 400
                }
        )
        Cfg.register({"cloudwatch.alarm00.configuration_url,Stable": {
            "DefaultValue" : "",
            "Format"       : "MetaString",
            "Description"  : """Alarm specification to track for scaling decisions.

    Ex: internal:ec2.scaleup.alarm-cpu-gt-75pc.yaml,Points=1001,BaselineThreshold=30.0

See [Alarm specification documentation](ALARMS_REFERENCE.md)  for more details.
            """
            }})
        for i in range(1, Cfg.get_int("cloudwatch.alarms.max_per_instance")):
            Cfg.register({"cloudwatch.alarm%02d.configuration_url,Stable" % i: {
                "DefaultValue": "",
                "Format"      : "MetaString",
                "Description" : """See `cloudwatch.alarm00.configuration_url`.
                """
                }})
        self.register_metric([
                     { "MetricName": "Cloudwatch.GetMetricData",
                       "Unit": "Count",
                       "StorageResolution": 60 
                     }])

        self.ec2.register_state_aggregates([
            {
                "Prefix": "cloudwatch.dashboard.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("cloudwatch.default_ttl"),
                "Exclude" : []
            }
            ])

    def get_prerequisites(self):
        now    = self.context["now"]
        client = self.context["cloudwatch.client"]


        # Read all CloudWatch alarm templates into memory
        alarm_definitions = {}
        for i in range(0, Cfg.get_int("cloudwatch.alarms.max_per_instance")):
            key = "cloudwatch.alarm%02d.configuration_url" % (i)
            r = Cfg.get_extended(key)
            if not r["Success"] or r["Value"] == "":
                continue

            d    = misc.parse_line_as_list_of_dict(r["Value"])
            url  = d[0]["_"]
            meta = d[0]

            index = "%02d" % i
            alarm_defs = {
                    "Index": index,
                    "Key": key,
                    "Url": url,
                    "Definition" : r,
                    "Metadata" : meta
                }

            prefix = "alarmname:"
            if url.startswith(prefix):
                alarm_defs["AlarmName"] = url[len(prefix):]
            else:
                log.log(log.NOTICE, "Read Alarm definition: %s" % r["Value"])
                try:
                    resp = misc.get_url(url.format(**self.context))
                    if resp is None:
                        raise Exception("URL content = <None>")
                    alarm_defs["Content"] = str(resp, "utf-8")
                except Exception as e:
                    log.exception("Failed to load Alarm definition '%s' : %e" % (r["Value"], e))
                    continue
            alarm_definitions[index] = alarm_defs

        self.alarm_definitions = alarm_definitions


        # Read all existing CloudWatch alarms
        alarms = []
        response = None
        while (response is None or "NextToken" in response):
            response = client.describe_alarms(
                MaxRecords=Cfg.get_int("cloudwatch.describe_alarms.max_results"),
                NextToken=response["NextToken"] if response is not None else ""
            )
            #log.debug(Dbg.pprint(response))
            for alarm in response["MetricAlarms"]:
                alarm_name = alarm["AlarmName"]
                alarm_def  = self.get_alarm_configuration_by_name(alarm_name)
                if alarm_def is not None:
                    # This is an alarm thats belong to this CloneSquad instance
                    alarms.append(alarm)
        #log.debug(Dbg.pprint(alarms))
        self.alarms = alarms

        # Sanity check
        for index in self.alarm_definitions.keys():
            alarm_def = self.alarm_definitions[index]
            if "AlarmName" not in alarm_def:
                continue
            alarm = next(filter(lambda a: a["AlarmName"] == alarm_def["AlarmName"], self.alarms), None)
            if alarm is None:
                log.warning("Alarm definition [%s](%s => %s) doesn't match an existing CloudWatch alarm!" % 
                        (alarm_def["Definition"]["Key"], alarm_def["Definition"]["Value"], alarm_def["Definition"]["Status"]))
        


        # Read all metrics associated with alarms


        # CloudWatch intense polling can be expensive: This algorithm links CW metric polling rate to the
        #    scale rate => Under intense scale up condition, polling is aggresive. If not, it falls down
        #    to one polling every 'cloudwatch.metrics.low_rate_polling_interval' seconds
        # TODO(@jcjorel): Avoid this kind of direct references to an upper level module!!
        integration_period        = Cfg.get_duration_secs("ec2.schedule.horizontalscale.integration_period")
        instance_scale_score      = self.ec2.get_integrated_float_state("ec2.schedule.scaleout.instance_scale_score", integration_period)

        self.metric_cache         = self.get_metric_cache()

        query = {
                "IdMapping": {},
                "Queries"  : []
            }

        # Build query for Alarm metrics
        if Cfg.get("ec2.schedule.desired_instance_count") == "-1":
            # Sort by oldest alarms first in cache
            cached_metric_names   = [ m["_MetricId"] for m in self.metric_cache]
            valid_alarms          = []
            for a in alarms:
                alarm_name = a["AlarmName"]
                alarm_def  = self.get_alarm_configuration_by_name(alarm_name)
                if alarm_def is None or alarm_def["AlarmDefinition"]["Url"].startswith("alarmname:"):
                    continue
                a["_SamplingTime"] = self.get_metric_by_id(alarm_name)["_SamplingTime"] if alarm_name in cached_metric_names else str(misc.epoch())
                valid_alarms.append(a)
            sorted_alarms = sorted(valid_alarms, key=lambda a: misc.str2utc(a["_SamplingTime"]))

            # We poll from the oldest to the newest and depending on the instance_scale_score to limit CloudWacth GetMetricData costs
            time_for_full_metric_refresh  = max(Cfg.get_duration_secs("cloudwatch.metrics.time_for_full_metric_refresh"), 1)
            app_run_period                = Cfg.get_duration_secs("app.run_period")
            minimum_polled_alarms_per_run = Cfg.get_int("cloudwatch.metrics.minimum_polled_alarms_per_run")
            maximum_polled_alarms_per_run = app_run_period / time_for_full_metric_refresh
            maximum_polled_alarms_per_run = min(maximum_polled_alarms_per_run, 1.0)
            weight                        = min(instance_scale_score, maximum_polled_alarms_per_run)
            max_alarms_for_this_run       = max(minimum_polled_alarms_per_run, int(min(weight, 1.0) * len(sorted_alarms)))
            for alarm in sorted_alarms[:max_alarms_for_this_run]:
                alarm_name          = alarm["AlarmName"]
                CloudWatch._format_query(query, alarm_name, alarm)

            # We always poll user supplied alarms
            for alarm in alarms:
                alarm_name          = alarm["AlarmName"]
                alarm_def = self.get_alarm_configuration_by_name(alarm_name)
                if alarm_def is None:
                    continue # Unknown alarm name
                if not alarm_def["AlarmDefinition"]["Url"].startswith("alarmname:"):
                    continue
                CloudWatch._format_query(query, alarm_name, alarm)

        max_retention_period = Cfg.get_duration_secs("cloudwatch.metrics.cache.max_retention_period")

        # Query Metric for Burstable instances
        instance_minimum_age_for_cpu_credit_polling = Cfg.get_duration_secs("cloudwatch.metrics.instance_minimum_age_for_cpu_credit_polling")
        burstable_instances = self.ec2.get_burstable_instances(State="running", ScalingState="-error")
        cpu_credit_polling  = 0
        for i in burstable_instances:
            instance_id   = i["InstanceId"]
            if (now - i["LaunchTime"]).total_seconds() < instance_minimum_age_for_cpu_credit_polling:
                continue
            cached_metric = self.get_metric_by_id("CPUCreditBalance/%s" % instance_id)
            if cached_metric is not None:
                # Note: Polling of CPU Credit Balance is a bit tricky as this API takes a lot of time to update and sometime
                #   do send back results from time to time. So we need to try multiple times...
                if ("_LastSamplingAttempt" in cached_metric and 
                        (now - misc.str2utc(cached_metric["_LastSamplingAttempt"])).total_seconds() < misc.str2duration_seconds("minutes=1")):
                    continue # We do not want to poll more than one per minute
                if (now - misc.str2utc(cached_metric["_SamplingTime"])).total_seconds() < max_retention_period * 0.8:
                    # Current data point is not yet expired. Keep of this attempt
                    cached_metric["_LastSamplingAttempt"] = now
                    continue
            cpu_credit_polling += 1
            CloudWatch._format_query(query, "%s/%s" % ("CPUCreditBalance", instance_id), {
                    "MetricName": "CPUCreditBalance",
                    "Namespace" : "AWS/EC2",
                    "Dimensions": [{
                        "Name": "InstanceId",
                        "Value": instance_id
                    }],
                    "Period": 300,
                    "Statistic"  : "Average"
                })
        log.log(log.NOTICE, "Will poll %d instances for CPU Credit balance." % cpu_credit_polling)

        # Make request to CloudWatch
        query_counter  = self.ec2.get_state_int("cloudwatch.metric.query_counter", default=0)
        queries        = query["Queries"]
        metric_results = []
        metric_ids     = []
        no_metric_ids  = []
        while len(queries) > 0:
            q        = queries[:500]
            queries  = queries[500:]
            results  = []
            response = None
            while response is None or "NextToken" in response:
                args = {
                        "MetricDataQueries" : q,
                        "StartTime" : now - timedelta(seconds=Cfg.get_duration_secs("cloudwatch.metrics.data_period")),
                        "EndTime" : now
                  }
                if response is not None: args["NextToken"] = response["NextToken"]
                response = client.get_metric_data(**args)
                results.extend(response["MetricDataResults"])
                query_counter += len(q)

            for r in results:
                if r["StatusCode"] != "Complete":
                    log.error("Failed to retrieve metrics: %s" % q)
                    continue
                metric_id = query["IdMapping"][r["Id"]]
                if len(r["Timestamps"]) == 0:
                    if metric_id not in no_metric_ids: no_metric_ids.append(metric_id)
                    continue
                if metric_id not in metric_ids: metric_ids.append(metric_id)
                r["_MetricId"]     = metric_id
                r["_SamplingTime"] = str(now)
                log.debug(r)
                metric_results.append(r)
        if len(no_metric_ids):
            log.info("No metrics returned for alarm '%s'" % no_metric_ids)

        # Merge with existing cache metric
        metric_cache      = self.metric_cache
        self.metric_cache = metric_results
        for m in metric_cache:
            if m["_MetricId"] in metric_ids or "_SamplingTime" not in m: 
                continue
            if (now - misc.str2utc(m["_SamplingTime"])).total_seconds() < max_retention_period:
                    self.metric_cache.append(m)

        self.ec2.set_state("cloudwatch.metric.query_counter"  , query_counter,     TTL=Cfg.get_duration_secs("cloudwatch.default_ttl"))
        self.ec2.set_state_json("cloudwatch.metrics.cache"    , self.metric_cache, TTL=Cfg.get_duration_secs("cloudwatch.default_ttl"))
        self.set_metric("Cloudwatch.GetMetricData", query_counter)

        # Augment Alarm definitions and Instances with associated metrics
        for metric in self.metric_cache:
            metric_id  = metric["_MetricId"]

            alarm_data = self.get_alarm_data_by_name(metric_id)
            if alarm_data is not None : 
                alarm_data["MetricDetails"] = metric
                continue
            
            instance   = next(filter(lambda i: "CPUCreditBalance/%s" % i["InstanceId"] == metric_id, burstable_instances), None)
            if instance is not None:
                instance["_Metrics"] = {}
                instance["_Metrics"]["CPUCreditBalance"] = metric
                continue


    def _format_query(query, metric_id, metric):
        uniq_id                      = "id%s" % misc.sha256(metric_id)
        query["IdMapping"][uniq_id] = metric_id
        q = {
                "Id" : uniq_id ,
                "MetricStat" : {
                        "Metric" : {
                                "MetricName" : metric["MetricName"],
                                "Namespace"  : metric["Namespace"]
                            },
                        "Period" : metric["Period"],
                        "Stat"   : metric["Statistic"]
                    },
                "ReturnData": True
            }
        if "Dimensions" in metric: q["MetricStat"]["Metric"]["Dimensions"] = metric["Dimensions"]
        if "Unit"       in metric: q["MetricStat"]["Unit"]       = metric["Unit"]
        query["Queries"].append(q)

    def get_metric_cache(self):
        return self.ec2.get_state_json("cloudwatch.metrics.cache", [])

    def get_metric_by_id(self, metric_id):
        return next(filter(lambda m: m["_MetricId"] == metric_id, self.metric_cache), None)

    def _get_alarm_name(self, group_name, instance_id, index):
        return "CloneSquad-%s-%s-%02d" % (group_name, instance_id, index)

    def get_alarm_names_with_metrics(self):
        return [ a["AlarmName"] for a in self.alarms ]

    def get_alarm_data_by_name(self, alarm_name):
        return next(filter(lambda alarm: alarm["AlarmName"] == alarm_name, self.alarms), None)

    def get_alarm_configuration_by_name(self, alarm_name):
        # First) Try to detect a CloneSquad managed alarm
        try:
            m = re.search('^CloneSquad-%s-(i-[0-9a-z]+)-(\d\d)$' % (self.context["GroupName"]), alarm_name)
            return {
                "InstanceId"      : m.group(1),
                "AlarmDefinition" : self.alarm_definitions[m.group(2)]
            }
        except: pass

        # Second) Try to lookup an alarmname definition (regex based)
        for alarm_idx in self.alarm_definitions:
            alarm_def = self.alarm_definitions[alarm_idx]
            if "AlarmName" in alarm_def:
                if re.match(alarm_def["AlarmName"], alarm_name) is not None:
                    return {
                        "AlarmName"       : alarm_def["AlarmName"],
                        "AlarmDefinition" : alarm_def
                    }
        return None

    @xray_recorder.capture()
    def configure_alarms(self):
        """ Configure Cloudwatch Alarms for each instance.

            The algorithm needs to manage missing alarm as well updating existing alarms
        """
        now    = self.context["now"]
        client = self.context["cloudwatch.client"]

        valid_alarms = []
        nb_of_updated_alarms = 0
        max_update_per_batch = Cfg.get_int("cloudwatch.metrics.max_update_per_batch")

        log.log(log.NOTICE, "Found following Alarm definition key(s) in configuration: %s" % [d for d in self.alarm_definitions])

        # Step 1) Create or Update CloudWatch Alarms for running instances
        for instance in self.ec2.get_instances(State="pending,running", ScalingState="-error,draining,excluded"):
            instance_id = instance["InstanceId"]

            age_secs         = (now - instance["LaunchTime"]).total_seconds()
            min_instance_age = Cfg.get_duration_secs("cloudwatch.alarms.min_instance_age")
            if age_secs < min_instance_age:
                log.log(log.NOTICE, "Instance '%s' too young. Wait %d seconds before to set an alarm..." % 
                        (instance_id, min_instance_age - age_secs))
                continue

            #Update alarms for this instance
            for alarm_definition in self.alarm_definitions:
                # First, check if an alarm already exists
                alarm_name = self._get_alarm_name(self.context["GroupName"], instance["InstanceId"], int(alarm_definition))
                existing_alarms = list(filter(lambda x: x['AlarmName'] == alarm_name, self.alarms))

                # Load alarm template
                try:
                    if "Content" not in self.alarm_definitions[alarm_definition]:
                        continue
                    kwargs = self.context.copy()
                    kwargs["InstanceId"] = instance_id
                    alarm_template = self.alarm_definitions[alarm_definition]["Content"].format(**kwargs)
                    alarm = yaml.safe_load(alarm_template)
                except Exception as e:
                    log.exception("[ERROR] Failed to read YAML alarm file '%s' : %s" % (alarm_template, e))
                    continue
                alarm["AlarmName"] = alarm_name

                valid_alarms.append(alarm_name)

                #Check if an alarm already exist
                existing_alarm = None
                if len(existing_alarms) > 0:
                    existing_alarm = existing_alarms[0]
                    
                    # Check if alarm definition will be the same
                    a = {**existing_alarm, **alarm}
                    # 2020/07/20: CloudWatch Alarm API does not return Tags. Have to deal with
                    #  while comparing the configurations.
                    if "Tags" in a and "Tags" not in existing_alarm:
                        del a["Tags"]
                    if a == existing_alarm:
                        #log.debug("Not updating alarm '%s' as configuration is already ok" % alarm_name)
                        continue

                    # Check if we updated this alarm very recently
                    delta = datetime.now(timezone.utc) - existing_alarm["AlarmConfigurationUpdatedTimestamp"] 
                    if delta < timedelta(minutes=1):
                        log.debug("Alarm '%s' updated to soon" % alarm_name)
                        continue

                nb_of_updated_alarms += 1
                if nb_of_updated_alarms > max_update_per_batch: break

                log.log(log.NOTICE, "Updating/creating CloudWatch Alarm '%s' : %s" % (alarm_name, alarm))
                resp = client.put_metric_alarm(**alarm)
                log.debug(Dbg.pprint(resp))

        # Step 2) Destroy CloudWatch Alarms for non existing instances (Garbage Collection)
        for existing_alarm in self.alarms:
            alarm_name = existing_alarm["AlarmName"]
            if not alarm_name.startswith("CloneSquad-%s-i-" % (self.context["GroupName"])):
                continue
            if alarm_name not in valid_alarms:
                nb_of_updated_alarms += 1
                if nb_of_updated_alarms > max_update_per_batch: break
                log.debug("Garbage collection orphan Cloudwatch Alarm '%s'" % alarm_name)
                resp = client.delete_alarms(AlarmNames=[alarm_name])
                log.debug(resp)
                nb_of_updated_alarms += 1
                if nb_of_updated_alarms > max_update_per_batch: break

    def register_metric(self, spec):
        self.metrics.extend(spec)

    def sent_metrics(self):
        return self.metrics

    def set_metric(self, name, value, dimensions=None):
        metric = None
        if dimensions is None:
            m = next(filter(lambda m: m["MetricName"] == name and "Dimensions" not in m, self.metrics), None)
        else:
            m = next(filter(lambda m: m["MetricName"] == name and "Dimensions" in m and m["Dimensions"] == dimensions, self.metrics), None)
        if m is None:
            raise Exception("Unknown metric set '%s' (Dimensions=%s)"  % (name, dimensions))

        m["Value"]     = float(value) if value is not None else None
        m["Timestamp"] = self.context["now"]
        m["Dimensions"] = [{
                "Name": "GroupName",
                "Value": self.context["GroupName"]
                }]
        if dimensions is not None:
            m["Dimensions"].extend(dimensions)
        log.log(log.NOTICE, "Metric[%s] = %s (Dimensions=%s)" % (name, value, dimensions))

    @xray_recorder.capture()
    def send_metrics(self):
        client = self.context["cloudwatch.client"]
        namespace = "%s" % (Cfg.get("cloudwatch.metrics.namespace"))
        subnamespace = Cfg.get("cloudwatch.metrics.subnamespace")
        if subnamespace != "":
            namespace += "/" + subnamespace

        excluded_metrics = Cfg.get_list_of_dict("cloudwatch.metrics.excluded")
        metrics = []
        for m in self.metrics:
            metric_name   = m["MetricName"]
            match_pattern = next(filter(lambda em: re.match(em["_"], metric_name), excluded_metrics), None)
            if match_pattern in excluded_metrics:
                log.log(log.NOTICE, "Metric '%s' excluded by keyword '%s' in 'cloudwatch.metrics.excluded'!" % (metric_name, match_pattern["_"]))
                continue
            if "Value" not in m:
                log.warning("Missing value for metric '%s'!" % metric_name)
                continue
            if m["Value"] is None:
                log.debug("Metric '%s' is disabled" % metric_name)
                continue
            metrics.append(m)

        log.log(log.NOTICE, "Sending %d metrics to Cloudwatch..." % len(metrics))
        while len(metrics):
            try:
                response = client.put_metric_data(Namespace=namespace, MetricData=metrics[:20])
            except:
                log.exception("Failed to send metrics to CloudWatch : %s" % metrics[:20])
            metrics = metrics[20:]



    #####
    # Dashboard management
    #####

    def _get_dashboard_name(self):
        return "CS-%s" % (self.context["GroupName"])

    def get_dashboard_images(self):
        dashboard = json.loads(self.load_dashboard())
        # Get graph properties
        graph_metrics = list(filter(lambda g: g["type"] == "metric", dashboard["widgets"]))
        properties    = [ g["properties"] for g in graph_metrics]

        client = self.context["cloudwatch.client"]

        r = {}
        for p in properties:
            title = p["title"]
            p["width"]  = Cfg.get_int("cloudwatch.dashboard.snapshot_width")
            p["height"] = Cfg.get_int("cloudwatch.dashboard.snapshot_height")
            try: 
                response = client.get_metric_widget_image(
                   MetricWidget=json.dumps(p)
                )
                r[title] = str(base64.b64encode(response["MetricWidgetImage"]),"utf-8")
            except Exception as e:
                log.exception("Failed to retrieve CloudWatch graph image for '%s'! : % e" % (title, e))
        return r

    def load_dashboard(self):
        dashboard = misc.get_url("internal:standard-dashboard.json")
        content = str(dashboard, "utf-8")
        for k in self.context:
            content = content.replace("{%s}" % k, str(self.context[k]))
        for k in Cfg.keys():
            content = content.replace("{%s}" % k, Cfg.get(k))
        return content

    @xray_recorder.capture()
    def configure_dashboard(self):
        client = self.context["cloudwatch.client"]
        # Cloudwatch service is billing calls to dashboard API. We make sure that we do not call it too often
        now                      = self.context["now"]
        dashboard_state          = Cfg.get_int("cloudwatch.dashboard.use_default")
        dashboard_last_state     = self.ec2.get_state("cloudwatch.dashboard.use_default.last_state")
        self.ec2.set_state("cloudwatch.dashboard.use_default.last_state", dashboard_state, TTL=Cfg.get_duration_secs("cloudwatch.default_ttl"))

        last_dashboad_action     = self.ec2.get_state_date("cloudwatch.dashboard.last_action", default=misc.epoch())
        dashboad_update_interval = Cfg.get_duration_secs("cloudwatch.dashboard.update_interval")
        if (str(dashboard_state) == dashboard_last_state) and (now - last_dashboad_action).total_seconds() < dashboad_update_interval:
            log.debug("Not yet the time to manage the dashboard.")
            return

        if Cfg.get_int("cloudwatch.dashboard.use_default") != 1:
            try:
                client.delete_dashboards(
                        DashboardNames=[self._get_dashboard_name()]
                    )
            except: 
                pass
        else:
            content = self.load_dashboard()
            log.log(log.NOTICE, "Configuring CloudWatch dashboard '%s'..." % self._get_dashboard_name())

            response = client.put_dashboard(
                    DashboardName=self._get_dashboard_name(),
                    DashboardBody=content
                )
        self.ec2.set_state("cloudwatch.dashboard.last_action", now, TTL=Cfg.get_duration_secs("cloudwatch.default_ttl"))


