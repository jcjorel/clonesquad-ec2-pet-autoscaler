import os
import math
import boto3
import yaml
import pdb
import re
import arrow
from datetime import datetime
from datetime import timedelta
from collections import defaultdict

import notify
from notify import record_call as R
import sqs
import kvtable
import scheduler
import misc
import config as Cfg
import debug as Dbg

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class Scheduler:
    def __init__(self, context=None, ec2=None, cloudwatch=None):
        self.context                = context
        self.ec2                    = ec2
        self.cloudwatch             = cloudwatch
        self.scheduler_table        = None
        self.event_names            = []
        self.rules                  = [] 
        self.local_now              = None
        Cfg.register({
            "cron.max_rules_per_batch": "10",
            "scheduler.cache.max_age": "seconds=60",
            "cron.disable": "0",
            "backup.cron,Stable": {
                "DefaultValue": "cron(0 * * * ? *)",
                "Format": "String",
                "Description": """Cron job specification for [Backup and Metadata](BACKUP_AND_METADATA.md) generation.

This setting control when Configuration/Scheduler DynamoDB tables are backuped. It also defines when the Metadata files, queriable with AWS Athena, are generated. The format follows the `cron(...)` and `localcron(...)` keywords as defined in the [scheduler documentation](SCHEDULER.md).

By default, an hourly cron job is defined.

> Setting this parameter to `disabled` will disable this backup and metadata generation feature even if the Cloudformation parameter [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path) is defined.
            """
            },
            "backup.cron.inhibit_delay_after_install": "hours=1"
        })


    def get_prerequisites(self):
        if Cfg.get_int("cron.disable"):
            return

        # Get Timezone related info 
        self.local_now  = arrow.get(misc.local_now()) # Get local time (with local timezone)
        self.utc_offset = self.local_now.utcoffset()
        self.dst_offset = self.local_now.dst()
        self.tz         = self.local_now.strftime("%Z")
        log.log(log.NOTICE, "Current timezone offset to UTC: %s, DST: %s, TimeZone: %s" % 
                (self.utc_offset, self.dst_offset, self.tz))

        # Load scheduler KV table
        self.scheduler_table = kvtable.KVTable.create(self.context, self.context["SchedulerTable"],
                cache_max_age=Cfg.get_duration_secs("scheduler.cache.max_age"))

        # Compute event names
        self.load_event_definitions()

        # Read all existing event rules
        client = self.context["events.client"]
        params = {
           "NamePrefix": "CS-Cron-%s-" % (self.context["GroupName"]),
           "Limit":      10
        }
        self.rules = []
        paginator  = client.get_paginator('list_rules')
        response_iterator = paginator.paginate(**params)
        for response in response_iterator:
            if "Rules" in response: 
                self.rules.extend(response["Rules"])

        max_rules_per_batch = Cfg.get_int("cron.max_rules_per_batch")
        # Create missing rules
        expected_rule_names = [ r["Name"] for r in self.event_names]
        existing_rule_names = [ r["Name"] for r in self.rules]
        for r in expected_rule_names:
            if r not in existing_rule_names:
                max_rules_per_batch -= 1
                if max_rules_per_batch <= 0:
                    break
                rule_def            = self.get_ruledef_by_name(r)
                schedule_spec       = rule_def["Data"][0]["schedule"]
                schedule_expression = self.process_cron_expression(schedule_spec)
                log.log(log.NOTICE, f"Creating {r} {schedule_spec} => {schedule_expression}...")

                # In order to remove burden on user, we perform a sanity check about a wellknown 
                #    limitation of Cloudwatch.
                if schedule_expression.startswith("cron("):
                    expr = [ i for i in schedule_expression.replace("(", " ").replace(")"," ").split(" ") if i != ""]
                    if len(expr) != 7:
                        log.warn("Schedule rule '%s' has an invalid cron expression '%s' (too short cron syntax)! Ignore it..." % 
                                (rule_def["EventName"], schedule_expression))
                        continue
                    if (expr[5] != '?' and not expr[3] == '?') or (expr[3] != '?' and not expr[5] == '?'):
                        log.warn("Schedule rule '%s' has an invalid cron expression '%s'. " 
                        "You can't specify the Day-of-month and Day-of-week fields in the same cron expression. If you specify a value (or a *) in one of the fields, you must use a ? (question mark) in the other. """ %  (rule_def["EventName"], schedule_expression))
                        continue

                # Update Cloudwatch rule
                try:
                    response = client.put_rule(
                       Name=r,
                       Description="Schedule Event '%s': %s" % (rule_def["EventName"], rule_def["Event"]),
                       RoleArn=self.context["CloudWatchEventRoleArn"],
                       ScheduleExpression=schedule_expression,
                       State='ENABLED'
                    )
                    log.debug("put_rule: %s" % response)
                except Exception as e:
                    log.exception("Failed to create scheduler event '%s' (%s) : %s" % (r, schedule_expression, e))

                try:
                    response = client.put_targets(
                          Rule=r,
                          Targets=[{
                            'Arn': self.context["InteractLambdaArn"],
                            'Id': "id%s" % r,
                            }]
                      )
                    log.debug("put_targets: %s" % response)
                except Exception as e:
                    log.exception("Failed to set targets for event rule '%s' : %s" % (r, e))

        # Garbage collect obsolete rules
        for r in existing_rule_names:
            if r not in expected_rule_names:
                max_rules_per_batch -= 1
                if max_rules_per_batch <= 0:
                    break
                try:
                    client.remove_targets(Rule=r, Ids=["id%s" % r])
                    client.delete_rule(Name=r)
                except Exception as e:
                    log.exception("Failed to delete rule '%s' : %s" % (r, e))


    def process_cron_expression(self, expression, tz=None):
        """ Return an UTC cron expression based on local timezone supplied one.
        """

        m = re.search("localcron\((.*)\)", expression)
        if not m:
            log.debug(f"Not a local timezone CRON specification: {expression}.")
            return expression # No match
        cron_spec = m.group(1)
        try:
            minutes, hours, dom, month, dow, year = [s for s in cron_spec.split(" ") if s != ""]
        except:
            log.debug(f"Invalid format for local timezone CRON specification: {expression}.")
            return expression # Bad format

        converts = []
        for is_minute in [True, False]:
            converted = []
            items = minutes if is_minute else hours
            for item in items.split(","):
                if item == "": continue
                item_range = item.split("-")
                for i in range(0, len(item_range)):
                    r    = item_range[i]
                    try:
                        unit = int(r)
                        if is_minute:
                            dt = self.local_now.replace(minute=unit, second=0)
                        else:
                            dt = self.local_now.replace(hour=unit, second=0)
                            # Take into account odd TZ
                            dt = dt.shift(seconds=-(self.utc_offset.total_seconds() % 3600))
                        utc_now   = dt.to('utc')
                        item_range[i] = str(utc_now.minute) if is_minute else str(utc_now.hour)
                    except:
                        pass # Not an integrer. Let the item as-is
                converted.append("-".join(item_range))
            converts.append(",".join(converted))
        return f"cron(%s %s {dom} {month} {dow} {year})" % (converts[0], converts[1])

    def load_event_definitions(self):
        now = self.context["now"]
        def _append_entry(e, param, event_name=None):
            event_name  = f"CS-Cron-{group_name}"
            event_name += "-" + misc.sha256(f"{e}:%s:%s:%s" % (self.tz, self.dst_offset, param))[:10]
            try:
                self.event_names.append({
                    "Name": event_name,
                    "EventName": e,
                    "Event": param,
                    "Data": misc.parse_line_as_list_of_dict(param, leading_keyname="schedule")
                })
            except Exception as ex:
                log.exception("Failed to parse Scheduler event '%s' (%s) : %s" % (e, param, ex))

        group_name   = self.context["GroupName"]
        periodic_key = f"CS-PeriodicBackup-{group_name}"

        # Read Scheduler DynamoDB table
        self.scheduler_table.reread_table()
        self.events = self.scheduler_table.get_dict()
        for e in self.events:
            if e.startswith("#"):
                continue # Ignore commented out rules
            if not isinstance(self.events[e], str): 
                log.warning(f"Scheduler entry {e} must have a 'Value' of type 'string'")
                continue
            if e == periodic_key:
                log.warning(f"Scheduler entry {e} uses a reserved keyword!")
                continue
            _append_entry(e, self.events[e])

        # Create a dynamic event when backup is configured
        if len(self.context["MetadataAndBackupS3Path"]):
            inhibit_delay_after_install = Cfg.get_duration_secs("backup.cron.inhibit_delay_after_install")
            install_time                = misc.str2utc(self.context["InstallTime"])
            delay_since_last_install    = now - install_time
            enable_delay                = (install_time + timedelta(seconds=inhibit_delay_after_install)) - now
            backup_cron_spec            = Cfg.get("backup.cron")
            if backup_cron_spec != "disabled":
                if delay_since_last_install.total_seconds() < inhibit_delay_after_install:
                    log.info(f"Configuration/Scheduler backup cron job is temporarily disabled as latest CloneSquad install "
                            f"time ({install_time}) is too close. Backup Cron job will be automatically enabled in {enable_delay}.")
                else:
                    backup_cron_spec = self.process_cron_expression(backup_cron_spec) # localcron() support
                    _append_entry(periodic_key, f"{backup_cron_spec},interact:backup") 

    def get_ruledef_by_name(self, name):
        try:
            return next(filter(lambda e: e["Name"] == name, self.event_names))
        except:
            return None
            
    def manage_rule_event(self, event):
        if Cfg.get_int("cron.disable"):
            return
        if "source" in event and event["source"] == "aws.events" and event["detail-type"] == "Scheduled Event":
            # Triggered by an AWS CloudWatch Scheduled event. We look for a ParameterSet 
            #   request based on the ARN
            misc.initialize_clients(["events"], self.context)
            misc.load_prerequisites(self.context, ["o_scheduler"])
            for r in event["resources"]:
                log.debug("Processing Scheduled event '%s'..." % r)
                m = re.search("^arn:aws:events:[a-z-0-9]+:[0-9]+:rule/CS-Cron-%s-(.*)" % self.context["GroupName"], r)
                if m is not None and len(m.groups()) == 1:
                    rule_num = m.group(1)
                    log.info("Got event rule '%s'" % rule_num)
                    self.load_event_definitions()
                    rule_def = self.get_ruledef_by_name("CS-Cron-%s-%s" % (self.context["GroupName"], rule_num))
                    log.debug(rule_def)

                    ttl = None
                    try:
                        ttl = misc.str2duration_seconds(rule_def["TTL"]) if rule_def is not None and "TTL" in rule_def else None
                    except Exception as e:
                        log.exception("[WARNING] Failed to read 'TTL' value '%s'!" % (TTL))

                    interact_str = "interact:"
                    params = dict(rule_def["Data"][0])
                    for k in params:
                        if k in ["TTL", "schedule"]: 
                            continue
                        if k.startswith(interact_str):
                            path = k[len(interact_str):]
                            self.context["o_interact"].internal_caller_handler(path)
                        else:
                            Cfg.set(k, params[k], ttl=ttl)
            return True
        return False

    def exports_metadata_and_backup(self, export_url):
        now        = self.context["now"]
        account_id = self.context["ACCOUNT_ID"]
        group_name = self.context["GroupName"]

        # Export configuration 
        self.scheduler_table.reread_table() # Ensure that we are in sync with the DynamoDB table 
        self.scheduler_table.export_to_s3(f"{export_url}/backups", "scheduler", prefix=f"archive/{now}-")
        self.scheduler_table.export_to_s3(f"{export_url}/backups", "scheduler", prefix="latest-")

        # Export matadata
        self.scheduler_table.export_to_s3(f"{export_url}/metadata/scheduler", group_name, prefix="scheduler", athena_search_format=True)

if __name__ == '__main__':
    # Local timezone test case
    scheduler = Scheduler()
    for tz in ["local", "Europe/Paris", "Asia/Kolkata"]:
        scheduler.local_now = arrow.now(tz)
        scheduler.utc_offset = scheduler.local_now.utcoffset()
        scheduler.dst_offset = scheduler.local_now.dst()
        for exp in ["localcron(0 12 * * ? *)", "localcron( 0,10/* 12 * * ? * )", "localcron(0 0 * * ? *)", "localcron(0-12, 0-1,13 * * ? *)", 
                "localcron(* 10 * * ? *)", "cron(1 2 * * ? *)"]:
            print(f"{tz} : {exp} => %s" % scheduler.process_cron_expression(exp, tz=tz))
        print("Current timezone offset to UTC: %s, DST: %s" % (scheduler.utc_offset, scheduler.dst_offset))

