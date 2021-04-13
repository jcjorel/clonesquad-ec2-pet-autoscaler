import os
import re
import math
import sys
import json
import uuid
import time
import pdb
from datetime import datetime
from datetime import timedelta
import boto3

import config
import misc
import sqs
import ec2
import ssm
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
        ctx["InteractAPIGWUrl"]   = "https://dummy-apigw-url"
        ctx["CloudWatchEventRoleArn"] = "arn:aws:iam::%s:role/CloneSquad-%s-CWRole-%s" % (account_id, ctx["GroupName"], ctx["AWS_DEFAULT_REGION"])
        ctx["GenericInsufficientDataActions_SNSTopicArn"] = "arn:aws:sns:%s:%s:CloneSquad-CloudWatchAlarm-InsufficientData-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["GenericOkActions_SNSTopicArn"] = "arn:aws:sns:%s:%s:CloneSquad-CloudWatchAlarm-Ok-%s"  % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["ScaleUp_SNSTopicArn"] =  "arn:aws:sns:%s:%s:CloneSquad-CloudWatchAlarm-ScaleUp-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["InteractLambdaArn"]  = "arn:aws:lambda:%s:%s:function:CloneSquad-Interact-%s" % (ctx["AWS_DEFAULT_REGION"], account_id, ctx["GroupName"])
        ctx["AWS_LAMBDA_LOG_GROUP_NAME"] = "/aws/lambda/CloneSquad-Main-%s" % ctx["GroupName"]
        ctx["SSMLogGroup"] = "/aws/lambda/CloneSquad-SSM-%s" % ctx["GroupName"]
        ctx["CloneSquadVersion"] = "--Development--"


# Special treatment while started from SMA invoke loval
if misc.is_sam_local() or __name__ == '__main__':
    fix_sam_bugs()
    print("SAM Local Environment:")
    for env in os.environ:
        print("%s=%s" % (env, os.environ[env]))

# Avoid client initialization time during event processsing
misc.initialize_clients(["ec2", "cloudwatch", "events", "sqs", "sns", "dynamodb",  "ssm", "lambda",
    "elbv2", "rds", "resourcegroupstaggingapi", "transfer"], ctx)
log.debug("End of preambule.")

@xray_recorder.capture(name="app.init")
def init(with_kvtable=True, with_predefined_configuration=True):
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
    log.debug("o_state setup...")
    ctx["o_state"]           = state.StateManager(ctx)
    log.debug("o_ec2 setup...")
    ctx["o_ec2"]             = ec2.EC2(ctx, ctx["o_state"])
    log.debug("o_ssm setup...")
    ctx["o_ssm"]             = ssm.SSM(ctx)
    log.debug("o_targetgroup setup...")
    ctx["o_targetgroup"]     = targetgroup.ManagedTargetGroup(ctx, ctx["o_ec2"])
    log.debug("o_cloudwatch setup...")
    ctx["o_cloudwatch"]      = cloudwatch.CloudWatch(ctx, ctx["o_ec2"])
    log.debug("o_notify setup...")
    ctx["o_notify"]          = notify.NotifyMgr(ctx, ctx["o_state"], ctx["o_ec2"], ctx["o_targetgroup"], ctx["o_cloudwatch"])
    log.debug("o_ec2_schedule setup...")
    ctx["o_ec2_schedule"]    = ec2_schedule.EC2_Schedule(ctx, ctx["o_ec2"], ctx["o_targetgroup"], ctx["o_cloudwatch"])
    log.debug("o_scheduler setup...")
    ctx["o_scheduler"]       = scheduler.Scheduler(ctx, ctx["o_ec2"], ctx["o_cloudwatch"])
    log.debug("o_interact setup...")
    ctx["o_interact"]        = interact.Interact(ctx)
    log.debug("o_rds setup...")
    ctx["o_rds"]             = rds.RDS(ctx, ctx["o_state"], ctx["o_cloudwatch"])
    log.debug("o_transferfamily setup...")
    ctx["o_transferfamily"]  = transferfamily.TransferFamily(ctx, ctx["o_state"], ctx["o_cloudwatch"])


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

    ctx["now"] = misc.utc_now()
    ctx["FunctionName"] = "Main"

    log.info("New instance scheduling period (version=%s)." % (ctx.get("CloneSquadVersion")))
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
    misc.load_prerequisites(ctx, ["o_state", "o_ec2", "o_notify", "o_ssm", "o_cloudwatch", "o_targetgroup", 
        "o_ec2_schedule", "o_scheduler", "o_rds", "o_transferfamily"])

    # Remember 'now' as the last execution date
    ctx["o_ec2"].set_state("main.last_call_date", value=ctx["now"], TTL=Cfg.get_duration_secs("app.default_ttl"))

    Cfg.dump()

    # Perform actions:
    log.debug("Main processing.")
    log.debug("Main - prepare_ssm()")
    ctx["o_ssm"].prepare_ssm()
    log.debug("Main - manage_targetgroup()")
    ctx["o_targetgroup"].manage_targetgroup()
    log.debug("Main - schedule_instances()")
    ctx["o_ec2_schedule"].schedule_instances()
    log.debug("Main - configure_alarms()")
    ctx["o_cloudwatch"].configure_alarms()
    log.debug("Main - RDS - manage_subfleet()")
    ctx["o_rds"].manage_subfleet()
    log.debug("Main - TransferFamily - manage_subfleet()")
    ctx["o_transferfamily"].manage_subfleet()
    log.debug("Main - prepara_metrics()")
    ctx["o_ec2_schedule"].prepare_metrics()
    log.debug("Main - send_metrics()")
    ctx["o_cloudwatch"].send_metrics()
    log.debug("Main - send_events()")
    ctx["o_ec2_schedule"].send_events()

    log.debug("Main - configure_dashboard()")
    ctx["o_cloudwatch"].configure_dashboard()
    log.debug("Main - pregenerate_interact_data()")
    ctx["o_interact"].pregenerate_interact_data()

    # If we got woke up by SNS, acknowledge the message(s) now
    sqs.process_sqs_records(ctx, event)

    ctx["o_notify"].notify_user_arn_resources()

    # Send all pending SSM commands
    ctx["o_ssm"].send_commands()

    # Call me back if needed
    sqs.call_me_back_send()

    # DEBUG (Stop immedialty all faulty instances)
    #issues = ctx["o_ec2_schedule"].get_instances_with_issues()
    #if False and len(issues):
    #    do_stop = False
    #    pdb.set_trace()
    #    issues = ctx["o_ec2_schedule"].get_instances_with_issues()
    #    if do_stop:
    #        ctx["o_ec2"].stop_instances(issues)


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

    log.info("Processing start (version=%s)" % (ctx.get("CloneSquadVersion")))
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
    log.info("Processing start (version=%s)" % (ctx.get("CloneSquadVersion")))
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

    log.info("Processing start (version=%s)" % (ctx.get("CloneSquadVersion")))
    init()
    notify.do_not_notify = True # We do not want notification and event management in the Interact function

    #log.info(json.dumps(event))
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

def debug_main_handler(event, context):
    """ Used for debugging purpose.
    As it is looping for ever on the main_handler(), it simulates well an initialized Lambda node with Python
    context re-use.
    """
    while True:
        try:
            main_handler(event, context)
            time.sleep(10)
        except:
            log.exception("Go Exception:")
            pdb.set_trace() # debug_main_handler

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


