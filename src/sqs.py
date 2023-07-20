import os
import math
import sys
import inspect
import json
import pdb
from datetime import datetime
from datetime import timedelta

import config
import misc
import debug as Dbg
import config as Cfg

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

ctx = None

def seconds_since_last_call():
    if "main.last_call_date" not in ctx:
        return 0
    return (misc.utc_now() - misc.str2utc(ctx["main.last_call_date"], default=misc.epoch())).total_seconds()

def next_call_delay():
    global ctx
    expected_delay  = Cfg.get_duration_secs("app.run_period")
    last_call_delay = seconds_since_last_call()
    delta           = expected_delay - last_call_delay
    if delta < 0:
        return expected_delay
    return max(int(delta), 0)

@xray_recorder.capture()
def read_all_sqs_messages():
    messages   = []
    sqs_client = ctx["sqs.client"]
    while True:
        response = sqs_client.receive_message(
                QueueUrl=ctx["MainSQSQueue"],
                AttributeNames=['All'],
                MaxNumberOfMessages=10,
                VisibilityTimeout=Cfg.get_int("app.run_period"),
                WaitTimeSeconds=0
           )
        if "Messages" in response:
            messages.extend(response["Messages"])
        else:
            break
    return messages

@xray_recorder.capture()
def process_sqs_records(ctx, event, function=None, function_arg=None):
    if event is None:
        return False
    if "Records" not in event:
        return False

    processed = 0
    for r in event["Records"]:
        if r["eventSource"] == "aws:sqs":
            misc.initialize_clients(["sqs"], ctx)
            if function is None or function(r, function_arg):
                log.debug("Deleting SQS record...")
                processed += 1
                try:
                    sqs_client = ctx["sqs.client"]
                    queue_arn  = r["eventSourceARN"]
                    queue_name = queue_arn.split(':')[-1]
                    account_id = queue_arn.split(':')[-2]
                    response = sqs_client.get_queue_url(
                       QueueName=queue_name,
                       QueueOwnerAWSAccountId=account_id
                    )
                    response = sqs_client.delete_message(QueueUrl=response["QueueUrl"], 
                            ReceiptHandle=r["receiptHandle"])
                except Exception as e:
                    log.exception("[WARNING] Failed to delete SQS message %s : %e" % (r, e))
    return processed

