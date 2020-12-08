import os
import re
import math
import sys
import json
import uuid
import pdb
from datetime import datetime
from datetime import timedelta

import config
import misc
import sqs
import debug
import debug as Dbg
import config as Cfg
import kvtable

import cslog
log = cslog.logger(__name__)
log.debug("Starting daemon...")

import app

def get_period():
    default = 20
    period  = kvtable.KVTable.get_kv_direct("app.run_period", app.ctx["ConfigurationTable"], context=app.ctx, default=default)
    if period != None:
        try:
            return max(0, int(period))
        except Exception as e:
            log.exception("Failed to parse 'app.run_period'")
            return default


def eventloop():
    queue_attributes = sqs.get_queue_attributes()

    last_call = misc.epoch()
    event     = { "Records": [] }
    max_iter  = 100 # We do not want to run for ever in case of memory leaks
    period    = get_period()
    while max_iter:
        max_iter      -= 1

        execution_time = 0
        now            = misc.utc_now()
        if len(event["Records"]) or (now - last_call) > timedelta(seconds=period):
            try: 
                app.main_handler(event, None)
                event["Records"] = []
                last_call        = now
                execution_time   = (misc.utc_now() - now).total_seconds()
                log.info("main_handler() took %s seconds" % execution_time)
                if execution_time >= period:
                    log.warning("main_handler() execution time exceeds configured 'app.run_period' (=%s)! Consider increase this value!" % period)
            except Exception as e:
                log.exception("Got Exception while calling app.main_hanlder()! %s" % event)

        # Poll for SQS events
        log.log(log.NOTICE, "Entering SQS Queue polling...")
        while True:
            period    = get_period() # We need to read this value at each run to catch change quickly
            delta     = max(0, round(period - (misc.utc_now() - last_call).total_seconds()))
            timeout   = max(2, min(20, delta)) # 'timeout' must be between 2 and 20
            log.log(log.NOTICE, "Reading Main SQS queue with timeout=%d (app.run_period=%s)" % (timeout, period))
            messages = sqs.read_sqs_messages(timeout=timeout)
            if len(messages): 
                # Simulate a Lambda event structure for SQS
                log.log(log.NOTICE, "Received SQS messages : %s" % messages)
                for r in messages:
                    r.update({
                        "eventSource":   "aws:sqs",
                        "eventSourceARN": queue_attributes["QueueArn"],
                        "receiptHandle":  r["ReceiptHandle"]
                        })
                event["Records"].extend(messages)
                break
            if delta == 0:
                break


if __name__ == '__main__':
    misc.initialize_clients(["sqs", "dynamodb"], app.ctx)
    eventloop()

