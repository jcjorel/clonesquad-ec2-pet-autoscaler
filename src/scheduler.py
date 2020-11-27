import math
import boto3
import json
import pdb
import re
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
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class Scheduler:
    @xray_recorder.capture(name="SchedulerTable.__init__")
    def __init__(self, context, ec2, cloudwatch):
        self.context                = context
        self.ec2                    = ec2
        self.cloudwatch             = cloudwatch
        self.scheduler_table        = None
        self.event_names            = []
        self.rules                  = [] 
        Cfg.register({
            "cron.max_rules_per_batch": "10",
            "cron.disable": "0"
        })


    def get_prerequisites(self):
        if Cfg.get_int("cron.disable"):
            return
        self.scheduler_table        = kvtable.KVTable(self.context, self.context["SchedulerTable"])

        # Compute event names
        self.load_event_definitions()

        # Read all existing event rules
        client = self.context["events.client"]
        params = {
           "NamePrefix": "CS-Cron-%s-" % (self.context["GroupName"]),
           "Limit":      10
        }
        rules  = []
        while True:
            response = client.list_rules(**params)
            if "Rules" in response: rules.extend(response["Rules"])
            if "NextToken" not in response: break
            params["NextToken"] = response["NextToken"]
        self.rules = rules

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
                schedule_expression = rule_def["Data"][0]["schedule"] 

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
                    client.remove_targets(
                      Rule=r,
                      Ids=["id%s" % r]
                      )
                    client.delete_rule(Name=r)
                except Exception as e:
                    log.exception("Failed to delete rule '%s' : %s" % (r, e))

    def load_event_definitions(self):
        self.scheduler_table.reread_table()
        self.events = self.scheduler_table.get_dict()
        for e in self.events:
            if not isinstance(self.events[e], str): continue

            digest     = misc.sha256("%s:%s" % (e, self.events[e]))
            event_name = "CS-Cron-%s-%s" % (self.context["GroupName"], digest[:10])
            try:
                self.event_names.append({
                    "Name": event_name,
                    "EventName": e,
                    "Event": self.events[e],
                    "Data": misc.parse_line_as_list_of_dict(self.events[e], leading_keyname="schedule")
                })
            except Exception as ex:
                log.exception("Failed to parse Scheduler event '%s' (%s) : %s" % (e, self.events[e], ex))

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

                    log.info(Dbg.pprint(rule_def))
                    params = dict(rule_def["Data"][0])
                    for k in params:
                        if k in ["TTL", "schedule"]: continue
                        Cfg.set(k, params[k], ttl=ttl)
            return True
        return False


