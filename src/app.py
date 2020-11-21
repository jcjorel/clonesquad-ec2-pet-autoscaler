import os
import re
import math
import sys
import json
import uuid
import pdb
from datetime import datetime
from datetime import timedelta
import boto3

import config
import misc
import sqs
import ec2
import ec2_schedule
import sns
import cloudwatch
import targetgroup
import scheduler
import notify
import interact
import rds
import transferfamily
import state
from kvtable import KVTable
import debug
from notify import record_call as R
from notify import record_call_lt as RLT
import debug as Dbg
import config as Cfg

from aws_xray_sdk import global_sdk_config
global_sdk_config.set_sdk_enabled("AWS_XRAY_SDK_ENABLED" in os.environ and os.environ["AWS_XRAY_SDK_ENABLED"] in ["1", "True", "true"])
from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)
log.debug("App started.")

# Import environment variables
ctx         = {"now": misc.utc_now()}
sqs.ctx     = ctx
config.ctx  = ctx
for env in os.environ:
    ctx[env] = os.getenv(env)

def fix_sam_bugs():
        account_id = os.getenv("ACCOUNT_ID")
        # 2020/07/28: SAM local bug: DynamoDB tables and SNS Topics are not correctly propagated. Patch them manually
        ctx["ConfigurationTable"] = "CloneSquad-%s-Configuration" % (ctx["GroupName"])
        ctx["AlarmStateEC2Table"] = "CloneSquad-%s-AlarmState-EC2" % (ctx["GroupName"])
        ctx["StateTable"]      = "CloneSquad-%s-State" % (ctx["GroupName"])
        ctx["EventTable"]         = "CloneSquad-%s-EventLog" % (ctx["GroupName"])
        ctx["LongTermEventTable"] = "CloneSquad-%s-EventLog-LongTerm" % (ctx["GroupName"])
        ctx["SchedulerTable"]     = "CloneSquad-%s-Scheduler" % (ctx["GroupName"])
        ctx["MainSQSQueue"]       = "https://sqs.%s.amazonaws.com/%s/CloneSquad-Main-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["InteractSQSUrl"]     = "https://sqs.%s.amazonaws.com/%s/CloneSquad-Interact-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["CloudWatchEventRoleArn"] = "arn:aws:iam::%s:role/CloneSquad-%s-CWRole" % (account_id, ctx["GroupName"])
        ctx["GenericInsufficientDataActions_SNSTopicArn"] = "arn:aws:sns:%s:%s:CloneSquad-CloudWatchAlarm-InsufficientData-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["GenericOkActions_SNSTopicArn"] = "arn:aws:sns:%s:%s:CloneSquad-CloudWatchAlarm-Ok-%s"  % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["ScaleUp_SNSTopicArn"] =  "arn:aws:sns:%s:%s:CloneSquad-CloudWatchAlarm-ScaleUp-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["InteractLambdaArn"]  = "arn:aws:lambda:%s:%s:function:CloneSquad-Interact-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["AWS_LAMBDA_LOG_GROUP_NAME"] = "/aws/lambda/CloneSquad-Main-%s" % ctx["GroupName"]


# Special treatment while started from SMA invoke loval
if misc.is_sam_local() or __name__ == '__main__':
    fix_sam_bugs()
    print("SAM Local Environment:")
    for env in os.environ:
        print("%s=%s" % (env, os.environ[env]))

log.debug("End of preambule.")

@xray_recorder.capture(name="app.init")
def init(with_kvtable=True, with_predefined_configuration=True):
    log.debug("Init.")
    config.init(ctx, with_kvtable=with_kvtable, with_predefined_configuration=with_predefined_configuration)
    Cfg.register({
           "app.run_period,Stable" : {
               "DefaultValue": "seconds=20",
               "Format"      : "Duration",
               "Description" : """Period when the Main scheduling Lambda function is run.

The smaller, the more accurate and reactive is CloneSquad. The bigger, the cheaper is CloneSquad to run itself (Lambda executions,
Cloudwatch GetMetricData, DynamoDB queries...)
               """
           },
           "app.default_ttl" : "300",
           "app.disable,Stable": {
                "DefaultValue": 0,
                "Format": "Bool",
                "Description": """Flag to disable Main Lambda function responsible to start/stop EC2 instances. 

It disables completly CloneSquad. While disabled, the Lambda will continue to be started every minute to test
if this flag changed its status and allow normal operation again."""
               },
           "app.archive_interact_events": "0"
        })

    log.debug("Setup management objects.")
    o_state           = state.StateManager(ctx)
    o_ec2             = ec2.EC2(ctx, o_state)
    o_targetgroup     = targetgroup.ManagedTargetGroup(ctx, o_ec2)
    o_cloudwatch      = cloudwatch.CloudWatch(ctx, o_ec2)
    o_notify          = notify.NotifyMgr(ctx, o_state, o_ec2, o_targetgroup, o_cloudwatch)
    o_ec2_schedule    = ec2_schedule.EC2_Schedule(ctx, o_ec2, o_targetgroup, o_cloudwatch)
    o_scheduler       = scheduler.Scheduler(ctx, o_ec2, o_cloudwatch)
    o_interact        = interact.Interact(ctx)
    o_rds             = rds.RDS(ctx, o_state, o_cloudwatch)
    o_transferfamily  = transferfamily.TransferFamily(ctx, o_state, o_cloudwatch)
    ctx.update({
        "o_state"         : o_state,
        "o_ec2"           : o_ec2,
        "o_targetgroup"   : o_targetgroup,
        "o_cloudwatch"    : o_cloudwatch,
        "o_notify"        : o_notify,
        "o_ec2_schedule"  : o_ec2_schedule,
        "o_scheduler"     : o_scheduler,
        "o_interact"      : o_interact,
        "o_rds"           : o_rds,
        "o_transferfamily": o_transferfamily
        })


@xray_recorder.capture()
def main_handler(event, context):
    log.debug("Handler start.")
    r = RLT(lambda args, kwargs, r: True, main_handler_entrypoint, event, context)
    # Persist all aggregated data
    xray_recorder.begin_subsegment("main_handler_entrypoint.persist_aggregates")
    KVTable.persist_aggregates()
    xray_recorder.end_subsegment()
    log.log(log.NOTICE, "Normal end.")
    return r

@xray_recorder.capture()
def main_handler_entrypoint(event, context):
    """

    Parameters
    ----------
    event: dict, required

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------

    """

    #print(Dbg.pprint(event))

    ctx["now"] = misc.utc_now()
    ctx["FunctionName"] = "Main"

    misc.initialize_clients(["ec2", "cloudwatch", "events", "sqs", "sns", "dynamodb", 
        "elbv2", "rds", "resourcegroupstaggingapi", "transfer"], ctx)
    init()

    if Cfg.get_int("app.disable") != 0 and not misc.is_sam_local():
        log.warning("Application disabled due to 'app.disable' key")
        return

    no_is_called_too_early = False
    # Manage Spot interruption as fast as we can
    if sqs.process_sqs_records(ctx, event, function=ec2.manage_spot_notification, function_arg=ctx):
        log.info("Managed Spot Interruption SQS record!")
        # Force to run now disregarding `app.run_period` as we have at least one Spot instance to 
        #   remove from target groups immediatly
        no_is_called_too_early = True
    
    # Check that we are not called too early
    #   Note: We peform a direct read to the KVTable to spare initialization time when the
    #   Lambda is called too early
    ctx["main.last_call_date"] = ctx["o_ec2"].get_state("main.last_call_date", direct=True)
    if ctx["main.last_call_date"] is None or ctx["main.last_call_date"] == "": 
        ctx["main.last_call_date"] = str(misc.epoch())

    if not no_is_called_too_early and is_called_too_early():
        log.log(log.NOTICE, "Called too early by: %s" % event)
        notify.do_not_notify = True
        sqs.process_sqs_records(ctx, event)
        sqs.call_me_back_send()
        return

    log.debug("Load prerequisites.")
    misc.load_prerequisites(ctx, ["o_state", "o_ec2", "o_notify", "o_cloudwatch", "o_targetgroup", 
        "o_ec2_schedule", "o_scheduler", "o_rds", "o_transferfamily"])

    # Remember 'now' as the last execution date
    ctx["o_ec2"].set_state("main.last_call_date", value=ctx["now"], TTL=Cfg.get_duration_secs("app.default_ttl"))

    Cfg.dump()

    # Perform actions:
    log.debug("Main processing.")
    ctx["o_targetgroup"].manage_targetgroup()
    ctx["o_ec2_schedule"].schedule_instances()
    ctx["o_ec2_schedule"].stop_drained_instances()
    ctx["o_cloudwatch"].configure_alarms()
    ctx["o_rds"].manage_subfleet()
    ctx["o_transferfamily"].manage_subfleet()
    ctx["o_ec2_schedule"].prepare_metrics()

    ctx["o_cloudwatch"].send_metrics()
    ctx["o_cloudwatch"].configure_dashboard()
    ctx["o_interact"].pregenerate_interact_data()

    # If we got woke up by SNS, acknowledge the message(s) now
    sqs.process_sqs_records(ctx, event)

    ctx["o_notify"].notify_user_arn_resources()

    # Call me back if needed
    sqs.call_me_back_send()


def sns_handler(event, context):
    """

    Parameters
    ----------
    event: dict, required

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------

    """
    global ctx
    ctx["now"] = misc.utc_now()
    log.log(log.NOTICE, "Handler start.")
    ctx["FunctionName"] = "SNS"

    misc.initialize_clients(["ec2", "sqs", "dynamodb"], ctx)
    init()
    misc.load_prerequisites(ctx, ["o_state", "o_notify", "o_targetgroup"])

    Cfg.dump()

    sns_mgr = sns.SNSMgr(ctx, ctx["o_ec2"])
    r       = sns_mgr.handler(event, context)

    # Persist all aggregated data
    KVTable.persist_aggregates()

    # Call me back if needed
    call_me_back_send()
    log.log(log.NOTICE, "Normal end.")

    return r

def discovery_handler(event, context):
    """

    Parameters
    ----------
    event: dict, required

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------

    """

    global ctx
    ctx["now"]          = misc.utc_now()
    ctx["FunctionName"] = "Discovery"
    discovery = misc.discovery(ctx)
    log.debug(discovery)
    return discovery

def interact_handler(event, context):
    log.log(log.NOTICE, "Handler start.")
    r = RLT(lambda args, kwargs, r: True, interact_handler_entrypoint, event, context)
    # Persist all aggregated data
    KVTable.persist_aggregates()
    log.log(log.NOTICE, "Normal end.")
    return r

def interact_handler_entrypoint(event, context):
    """

    Parameters
    ----------
    event: dict, required

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------

    """

    global ctx
    ctx["now"]           = misc.utc_now()
    ctx["FunctionName"]  = "Interact"

    init()
    notify.do_not_notify = True # We do not want notification and event management in the Interact function

    log.info(json.dumps(event))
    if ctx["LoggingS3Path"] != "" and Cfg.get_int("app.archive_interact_events"):
        s3path = "%s/InteractEvents/%s.json" % (ctx["LoggingS3Path"], ctx["now"])
        log.warning("Pushing Interact event in '%s'!" % s3path)
        misc.put_s3_object(s3path, Dbg.pprint(event))

    response = {}
    if ctx["o_interact"].handler(event, context, response):
        log.debug("API Gateway response: %s" % response)
    sqs.process_sqs_records(ctx, event)
    return response


def is_called_too_early():
    global ctx
    delay = Cfg.get_duration_secs("app.run_period")
    delta = sqs.seconds_since_last_call()
    if delta != -1 and delta < delay:
        if misc.is_sam_local():
            log.warning("is_called_too_early disabled because running in SAM!")
            return False
        log.log(log.NOTICE, "Called too early (now=%s, delay=%s => delta_seconds=%s)..." %
                (ctx["now"], delay, delta)) 
        return True
    return False

if __name__ == '__main__':
    # To ease debugging, the Lambda Python code can be started inside the DevKit
    event = None
    if len(sys.argv) <= 1:
         main_handler(event, None)
         sys.exit(0)
    log.info("Looking for '%s' entrypoint..." % sys.argv[1])
    func = globals()[sys.argv[1]]
    if len(sys.argv) == 3:
        event = json.load(open(sys.argv[2]))
    func(event, None)


