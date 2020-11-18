import sys
import boto3
import json
import pdb
import re
import gzip
import base64
import kvtable
from kvtable import KVTable
from datetime import datetime
import traceback

import misc
import config as Cfg
import debug as Dbg
import debug

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

this = sys.modules[__name__]
this.notify_mgr    = None
this.do_not_notify = False

def record_call(is_success_func, f, *args, **kwargs):
    return _record_call(True, is_success_func, f, *args, **kwargs)

def record_call_lt(is_success_func, f, *args, **kwargs):
    return _record_call(False, is_success_func, f, *args, **kwargs)

@xray_recorder.capture()
def _record_call(need_shortterm_record, is_success_func, f, *args, **kwargs):
    global this
    record = {}
    record["EventType"] = f.__name__ 
    record["Input"] = { 
            "*args": list(args),
            "**kwargs": dict(kwargs)
        }

    managed_exception = None
    xray_recorder.begin_subsegment("notifycall-call:%s" % f.__name__)
    try:
        r =  f(*args, **kwargs)
        record["Output"] = json.dumps(r, default=str)
    except Exception as e:
        managed_exception = e
        record["Except"] = {
                "Exception": traceback.format_exc(),
                "Stackstrace": traceback.extract_stack(),
                "Reason": json.dumps(e, default=str)
            }
    xray_recorder.end_subsegment()

    if managed_exception is not None:
        # Persist now all aggregated data to not lose them
        xray_recorder.begin_subsegment("notifycall-persist_aggregates:%s" % f.__name__)
        try:
            KVTable.persist_aggregates()
        except Exception as e:
            log.exception("Failed to persist aggregated date!")
        xray_recorder.end_subsegment()

    if this.notify_mgr is None or this.do_not_notify:
        log.debug("Do not write Event in event table: notify_mgr=%s, do_not_notify=%s" % (this.notify_mgr, do_not_notify))
        if managed_exception is not None:
            raise managed_exception
        return r

    ctx    = this.notify_mgr.context

    try:
        need_longterm_record = managed_exception is not None or not is_success_func(args, kwargs, r) if is_success_func is not None else False
    except Exception as e:
        log.exception("Got an exception while assessing long term event management : %s" % e)
        need_longterm_record = True

    # Try to catch the maximum available metadata to ease later diagnosis
    #    Protect against exceptions to ensure proper logging
    record["Metadata"] = {}
    xray_recorder.begin_subsegment("notifycall-build_metadata:%s" % f.__name__)
    try:
        this.notify_mgr.ec2.get_prerequisites(only_if_not_already_done=True)
        record["Metadata"]["EC2"] = {
                "AllInstanceDetails": this.notify_mgr.ec2.get_instances(),
                "AllInstanceStatuses" : this.notify_mgr.ec2.get_instance_statuses(),
                "DrainingInstances" : [i["InstanceId"] for i in this.notify_mgr.ec2.get_instances(ScalingState="draining")],
                "BouncedInstances"  : [i["InstanceId"] for i in this.notify_mgr.ec2.get_instances(ScalingState="bounced")],
                "ExcludedInstances" : [i["InstanceId"] for i in this.notify_mgr.ec2.get_instances(ScalingState="excluded")],
                "ErrorInstances"    : [i["InstanceId"] for i in this.notify_mgr.ec2.get_instances(ScalingState="error")],
                "ScalingStates"     : this.notify_mgr.ec2.get_all_scaling_states()
                }
    except Exception as e: 
        log.exception('Failed to create record["Metadata"]["EC2"] : %s' % e)
    xray_recorder.end_subsegment()

    xray_recorder.begin_subsegment("notifycall-build_metadata_targetgroup:%s" % f.__name__)
    try:
        this.notify_mgr.targetgroup.get_prerequisites(only_if_not_already_done=True)
        record["Metadata"]["TargetGroups"] = this.notify_mgr.targetgroup.get_targetgroups_info()
    except Exception as e: 
        log.exception('Failed to create record["Metadata"]["TargetGroups"] : %s' % e)
    xray_recorder.end_subsegment()

    for key in ["Metadata"]:
        zipped_bytes  = gzip.compress(bytes(json.dumps(record[key], default=str), "utf-8"))
        record[key] = str(base64.b64encode(zipped_bytes), "utf-8")

    now                  = misc.utc_now()
    now_seconds          = misc.seconds_from_epoch_utc()
    max_longterm_records = Cfg.get_int("notify.event.longterm.max_records")
    if max_longterm_records <= 0: 
        need_longterm_record = 0

    tables = [
                {
                    "Name"        : ctx["EventTable"],
                    "NeedWrite"   : need_shortterm_record,
                    "TTL"         : Cfg.get_duration_secs("notify.event.default_ttl"),
                    "DBImages"    : False,
                    "DebugReport" : False
                },
                {
                    "Name"      : ctx["LongTermEventTable"],
                    "NeedWrite" : need_longterm_record,
                    "TTL"       : Cfg.get_duration_secs("notify.event.longterm.ttl"),
                    "DBImages"  : True,
                    "DebugReport" : True
                },
            ]
    xray_recorder.begin_subsegment("notifycall-update_tables:%s" % f.__name__)
    for table in tables:
        if not table["NeedWrite"]:
            continue
        UpdateExpression  = "set EventSource=:entrypoint, EventType=:eventtype, InputData=:input, OutputData=:output, HandledException=:exception, " 
        UpdateExpression += "Metadata=:metadata, ExpirationTime=:expirationtime" 
        ExpressionAttributeValues={
           ':entrypoint': {'S': ctx["FunctionName"]},
           ':eventtype' : {'S': record["EventType"]},
           ':input'     : {'S': json.dumps(record["Input"], default=str)},
           ':output'    : {'S': json.dumps(record["Output"] if "Output" in record else {}, default=str)},
           ':exception' : {'S': json.dumps(record["Except"] if "Except" in record else "", default=str)},
           ':metadata'  : {'S': json.dumps(record["Metadata"], default=str)},
           ':expirationtime' : {'N': str(now_seconds + table["TTL"])}
        }
        if table["DBImages"]:
            # Insert snapshots of the CloudWatch dashboard
            try:
                log.log(log.NOTICE, "Generating snapshots for Dashboard graphs...")
                images = this.notify_mgr.cloudwatch.get_dashboard_images()
                for i in images:
                    compressed_name   = i.replace(" ", "")
                    UpdateExpression += ", Graph_%s_PNG=:graph%s" % (compressed_name, compressed_name)
                    ExpressionAttributeValues[":graph%s" % compressed_name] = {
                            'S': images[i]
                            }
                log.info("/!\ Generated CloudWatch dashboard PNG snapshots in DynamoDb table '%s' for further event analysis!" % table["Name"])
            except Exception as e:
                log.exception("Failed to retrieve CloudWatch snapshot images! : %s" % e)

        response = ctx["dynamodb.client"].update_item(
            Key={
                "EventDate" : {'S': str(now)}
                },
            UpdateExpression=UpdateExpression,
            ExpressionAttributeValues=ExpressionAttributeValues,
            ReturnConsumedCapacity='TOTAL',
            TableName=table["Name"],
        )

        log.debug(Dbg.pprint(response))
        log.log(log.NOTICE, "Written event '[%s] %s' to table '%s'." % (str(now), 
            record["EventType"], table["Name"]))

        # Keep under control the number of LongTerm items stored in DynamoDB table
        if need_longterm_record:
            longterm_item_eventdates = [ m["_"] for m in this.notify_mgr.state.get_metastring_list("notify.longterm.itemlist", default=[])]
            log.log(log.NOTICE, "Guessed number of records in LongTerm Event table : %d", len(longterm_item_eventdates))
            longterm_item_eventdates.append(str(now))
            nb_records_to_delete     = max(len(longterm_item_eventdates) - max_longterm_records, 0)
            for eventdate in longterm_item_eventdates[:nb_records_to_delete]:
                try:
                    response = ctx["dynamodb.client"].delete_item(
                        Key={
                            'EventDate': {'S': eventdate}
                        },
                        TableName=ctx["LongTermEventTable"]
                    )
                    log.debug(response)
                    log.log(log.NOTICE, 
                            "Purged LongTerm Event record '%s' as too many are already stored (notify.event.longterm.max_records=%d)" %
                                (eventdate, max_longterm_records))
                except Exception as e:
                    log.exception("Got exception while deleting LongTerm record '%s' : %e" % (eventdate, e))
            this.notify_mgr.state.set_state("notify.longterm.itemlist", ";".join(longterm_item_eventdates[nb_records_to_delete:]), 
                TTL=Cfg.get_duration_secs("notify.event.longterm.ttl"))
            try:
                KVTable.persist_aggregates()
            except Exception as e:
                log.exception("Got exception while persisting KVTables : %s" % e)

        # Manage Debug report export to S3
        url = ctx["LoggingS3Path"]
        if url != "" and table["DebugReport"] and Cfg.get_int("notify.debug.send_s3_reports"):
            xray_recorder.begin_subsegment("notifycall-publish_all_reports:%s" % f.__name__)
            if ctx["FunctionName"] == "Interact":
                # Avoid recursion if throwing from InteractFunction
                log.info("Publishing Debug reports synchronously...")
                debug.publish_all_reports(ctx, url, "notifymgr_report")
            else:
                client = ctx["sqs.client"]
                log.info("Notifying Interact SQS Queue '%s' for asynchronous debug report generation..." % ctx["InteractSQSUrl"])
                response = client.send_message(
                    QueueUrl=ctx["InteractSQSUrl"],
                    MessageBody=json.dumps({
                        "OpType" : "Debug/PublishReportNow",
                        "Events" : {
                            "Timestamp": str(ctx["now"])
                            }
                        })
                    )
                log.debug(response)
            xray_recorder.end_subsegment()

    xray_recorder.end_subsegment()
    if managed_exception is not None:
        raise managed_exception
    return r

class NotifyMgr:
    @xray_recorder.capture(name="NotifyMgr.__init__")
    def __init__(self, context, state, ec2, targetgroup, cloudwatch):
        global this
        this.do_not_notify   = False
        this.notify_mgr = self
        self.context    = context
        self.ec2        = ec2
        self.targetgroup= targetgroup
        self.cloudwatch = cloudwatch
        self.state      = state
        self.table_name = None
        Cfg.register({
           "notify.event.default_ttl"          : "minutes=5",
           "notify.event.longterm.max_records,Stable"  : {
               "DefaultValue" : 50,
               "Format"       : "Integer",
               "Description"  : """Maximum records to hold in the Event-LongTerm DynamodDB table

Setting this value to 0, disable logging to the LongTerm event table.
"""
           },
           "notify.event.longterm.ttl,Stable"  : {
               "DefaultValue" : "days=5",
               "Format"       : "Duration",
               "Description"  : """Retention time for Long-Term DynamoDB entries.

This table is used to deep-dive analysis of noticeable events encountered by a CloneSquad deployment. It is mainly used to
improve CloneSquad over time by allowing easy sharing of essential data for remote debugging.
               """
           },
           "notify.event.keep_acked_records"    : "0",
           "notify.debug.obfuscate_s3_reports" : "1",
           "notify.debug.send_s3_reports"      : "1"
        })
        self.state.register_aggregates([
            {
                "Prefix": "notify.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("notify.event.longterm.ttl"),
                "Exclude" : []
            }
            ])


    def get_prerequisites(self):
        self.table_name = self.context["EventTable"]
        return

    def ack_event_dates(self, event_dates):
        client     = self.context["dynamodb.client"]
        table_name = self.context["EventTable"]
        for date in event_dates:
            if Cfg.get_int("notify.event.keep_acked_records"):
                response = client.update_item(
                    Key={
                        "EventDate" : {'S': date}
                        },
                    UpdateExpression="set AckDate=:ackdate",
                    ExpressionAttributeValues={
                       ':ackdate': {'S': str(self.context["now"])}
                    },
                    ConditionExpression="attribute_exists(EventDate)",
                    ReturnConsumedCapacity='TOTAL',
                    TableName=table_name,
                )
            else:
                response = client.delete_item(
                    Key={
                        'EventDate': {'S': date}
                    },
                    TableName=table_name
                )
            log.debug(Dbg.pprint(response))

    @xray_recorder.capture()
    def notify_user_arn_resources(self):
        # Notify specified resources if needed
        user_notification_arns = self.context["UserNotificationArns"].split(",")
        notification_message   = {}
        for arn in user_notification_arns:
            if "*" in arn or "?" in arn:
                # Ignore an ARN pattern:
                continue
            if arn == "":
                continue

            m = re.search("^arn:[a-z]+:([a-z]+):([-a-z0-9]+):([0-9]+):(.+)", arn)
            if len(m.groups()) < 4:
                log.warning("Failed to parse User supplied notification ARN '%s'!" % arn)
                continue
            notification_message[arn]                 = {}
            notification_message[arn]["service"]      = m[1]
            notification_message[arn]["region"]       = m[2]
            notification_message[arn]["account_id"]   = m[3]
            notification_message[arn]["service_path"] = m[4]
            notification_message[arn]["content"]      = {
                        "Date" : misc.utc_now(),
                        "Metadata": {
                            "AckLambdaARN" : self.context["InteractLambdaArn"],
                            "AckSQSUrl"    : self.context["InteractSQSUrl"]
                            }
                    }

        if len(notification_message) == 0:
            return 

        try:
            dynamodb_client = self.context["dynamodb.client"]
            event_items = misc.dynamodb_table_scan(dynamodb_client, self.table_name)
        except Exception as e:
            log.exception("Failed to perform table scan on '%s' DynamodDB table! Notifications not sent... : %s " % (self.event_table, e))
            return

        # Flatten the structure to make it easily manageable
        events = []
        for e in event_items:
            if "AckDate" not in e or e["AckDate"] == "":
                events.append(e)
        events.sort(key=lambda x: datetime.fromisoformat(x["EventDate"]), reverse=True)

        if len(events) == 0:
            return

        event_types = []
        for e in events:
            if e["EventType"] not in event_types: event_types.append(e["EventType"])

        events_r = events.copy()
        events_r.reverse()

        for arn in notification_message.keys():
            # Verify that message is not too big to send
            content           = notification_message[arn]["content"]
            content["Events"] = events
            service           = notification_message[arn]["service"]
            region            = notification_message[arn]["region"]
            account_id        = notification_message[arn]["account_id"]
            service_path      = notification_message[arn]["service_path"]
            truncated_message = "Truncated to fit message size < 256kB"

            content_str = json.dumps(content, default=str)
            while len(content_str) >= 256*1024:
                for e in events_r:
                    if e["Metadata"] != truncated_message:
                        e["Metadata"] = truncated_message
                        break
                content_str = json.dumps(content, default=str)

            try:
                if service == "lambda":
                    self.call_lambda(arn, region, account_id, service_path, content_str, event_types)
                elif service == "sqs":
                    self.call_sqs(arn, region, account_id, service_path, content_str, event_types)
                elif service == "sns":
                    self.call_sns(arn, region, account_id, service_path, content_str, event_types)
            except Exception as e:
                log.warning("Failed to notify '%s'! Got Exception: %s" % (arn, e))

    @xray_recorder.capture()
    def call_lambda(self, arn, region, account_id, service_path, content, e):
        misc.initialize_clients(["lambda"], self.context)
        client = self.context["lambda.client"]
        log.info("Notifying asynchronously UserLambda '%s' for event '%s'..." % (arn, e))
        response = client.invoke(
            FunctionName=arn,
            InvocationType='Event',
            LogType='Tail',
            Payload=content
        )
        #log.debug(Dbg.pprint(response))

    @xray_recorder.capture()
    def call_sqs(self, arn, region, account_id, service_path, content, e):
        misc.initialize_clients(["sqs"], self.context)
        client = self.context["sqs.client"]
        response = client.get_queue_url(
            QueueName=service_path,
            QueueOwnerAWSAccountId=account_id
        )
        log.info("Notifying to SQS Queue '%s' for event '%s'..." % (arn, e))
        response = client.send_message(
            QueueUrl=response["QueueUrl"],
            MessageBody=content)
        #log.debug(Dbg.pprint(response))

    @xray_recorder.capture()
    def call_sns(self, arn, region, account_id, service_path, content, e):
        misc.initialize_clients(["sns"], self.context)
        client = self.context["sns.client"]
        log.info("Notifying to SNS Topic '%s' for event '%s'..." % (arn, e))
        response = client.publish(
            TopicArn=arn,
            Message=content,
            Subject="CloneSquad/%s event notification" % (self.context["GroupName"])
        )
        #log.debug(Dbg.pprint(response))

