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

def eventloop():
    last_call = None
    max_iter  = 100 # We do not want to run for ever in case of memory leaks
    while max_iter:
        max_iter -= 1
        # Get the desired run period
        period = kvtable.KVTable.get_kv_direct("app.run_period", app.ctx["ConfigurationTable"], context=app.ctx, default="20")
        if period != None:
            try:
                period = max(0, int(period))
            except Exception as e:
                log.exception("Failed to parse 'app.run_period'")

        now   = misc.utc_now()
        if last_call is None or (now - last_call) > timedelta(seconds=period):
            event = None
            try: 
                app.main_handler(event, None)
            except Exception as e:
                log.exception("Got Exception while calling app.main_hanlder()! %s" % messages)
            execution_time = (misc.utc_now() - now).total_seconds()
            log.info("main_handler() took %s seconds" % execution_time)
            if execution_time >= period:
                log.warn("main_handler() execution time exceeds configured 'app.run_period' (=%s)! Consider increase this value!" % period)
            last_call = now
        # Poll for SQS events
        messages = sqs.read_sqs_messages(timeout=2)
        if len(messages): pdb.set_trace()
        log.debug(messages)


if __name__ == '__main__':
    misc.initialize_clients(["sqs", "dynamodb"], app.ctx)
    eventloop()

