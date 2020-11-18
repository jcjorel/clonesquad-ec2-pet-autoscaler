import os
import json
import debug
import debug as Dbg
import config
import sqs
import misc

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class QueryCache:
    def __init__(self):
        self.last_call_date = None
        self.cache          = {}

    def check_cache_invalidation(self, context):
        last_call_date = context["o_ec2"].get_state("main.last_call_date", direct=True)
        if last_call_date != self.last_call_date:
            # The Main Lambda function run recently: Invalidate the whole to return accurate data.
            self.cache = {}
            self.last_call_date = last_call_date

    def get(self, url, variant=None, default=None):
        key = "%s_%s" % (url, variant)
        if key in self.cache:
            return self.cache[key]
        return default

    def put(self, url, answer, variant=None):
        key = "%s_%s" % (url, variant)
        self.cache[key]= {
            "body"      : answer["body"],
            "statusCode": answer["statusCode"]
        }

# We set a global object that will survive multiple Lambda invocation to build a simple in-memory cache
query_cache = QueryCache()

class Interact:
    def __init__(self, context):
        self.context = context
        self.commands = {
                "discovery"           : {
                    "interface": ["apigw"],
                    "cache": "none",
                    "clients:": [],
                    "prerequisites": [],
                    "func": self.discovery,
                },
                "notify/ackevent"          : {
                    "interface": ["sqs", "apigw"],
                    "cache": "none",
                    "clients:": ["dynamodb"],
                    "prerequisites": [], 
                    "func": self.process_ack_event_dates,
                },
                "configuration/dump"       : {
                    "cache": "none",
                    "interface": ["apigw"],
                    "clients:": ["dynamodb"],
                    "cache": True,
                    "prerequisites": [],
                    "func": self.dump_configuration,
                },
                "debug/publishreportnow"   : {
                    "interface": ["sqs"],
                    "cache": "none",
                    "clients:": ["ec2"],
                    "prerequisites": ["o_state", "o_ec2", "o_notify", "o_targetgroup", "o_scheduler"],
                    "func": debug.manage_publish_report,
                },
                "cloudwatch/getmetriccache": {
                    "interface": ["apigw"],
                    "cache": "global",
                    "clients:": ["ec2", "cloudwatch"],
                    "cache": True,
                    "prerequisites": ["o_state", "o_ec2", "o_notify", "o_cloudwatch"],
                    "func": self.cloudwatch_get_metric_cache
                }
                }

    def get_prerequisites(self):
        return

    def discovery(self, context, event, response):
        response["statusCode"]   = 200
        discovery = {
                "identity": event["requestContext"]["identity"],
                "discovery": misc.discovery(self.context)
            }
        response["body"]         = Dbg.pprint(discovery)
        #response["Content-Type"] = "application/json"
        return True

    def cloudwatch_get_metric_cache(self, context, event, response):
        response["statusCode"] = 200
        response["body"] = Dbg.pprint(context["o_cloudwatch"].get_metric_cache())
        return True

    def process_ack_event_dates(self, context, event, response):
        context["o_notify"].ack_event_dates(event["Events"])
        response["statusCode"] = 200
        response["body"]       = json.dumps({
            })
        return False

    def dump_configuration(self, context, event, response):
        response["statusCode"] = 200
        only_stable_keys       = "Unstable" not in event or event["Unstable"] != "True"
        response["body"]       = Dbg.pprint(config.dumps(only_stable_keys=only_stable_keys))
        return True

    def handler(self, event, context, response):
        global query_cache
        if "httpMethod" in event and "path" in event:
            response.update({
                  "isBase64Encoded" : False,
                  "statusCode": 500,
                  "headers": {
                      "Content-Type": "application/json"
                  },
                  "body": ""
                  })

            querystring = ""
            if "queryStringParameters" in event and event["queryStringParameters"] is not None:
                querystring = "&".join(event["queryStringParameters"])
                event.update(event["queryStringParameters"])

            log.log(log.NOTICE, "Received API Gateway message for path '%s'" % event["path"])

            # Normalize command format
            arg         = event["OpType"] if "OpType" in event else event["path"]
            path        = arg.lower().split("/")
            path_list   = list(filter(lambda x: x != "", path))
            command     = "/".join(path_list)
            if command not in self.commands.keys():
                response["statusCode"] = 404
                response["body"]       = "Unknown command '%s'" % (command)
                return True

            event["OpType"] = command
            log.log(log.NOTICE, "Processing API Gateway command '%s'" % (command))
            cmd = self.commands[command]
            if "apigw" not in cmd["interface"]:
                response["statusCode"] = 404
                response["body"]       = "Command not available through API Gateway"
                return True

            is_cacheable  = cmd["cache"] in ["global", "client"]
            if is_cacheable:
                cacheable_url = "%s?%s_%s" % (command, querystring, "" if cmd["cache"] == "global" else event["requestContext"]["identity"]["userArn"])
                query_cache.check_cache_invalidation(self.context)
                entry = query_cache.get(cacheable_url)
                if entry is not None:
                    response.update(entry)
                    log.log(log.NOTICE, "API Gateway query '%s' served from the cache..." % command)
                    return True
            misc.load_prerequisites(self.context, cmd["prerequisites"])
            if cmd["func"](self.context, event, response) and is_cacheable:
                query_cache.put(cacheable_url, response)
            return True

        elif self.context["o_scheduler"].manage_rule_event(event):
            log.log(log.NOTICE, "Processed Cloudwatch Scheduler event")

        elif sqs.process_sqs_records(self.context, event, function=self.sqs_interact_processing):
            log.info("Processed SQS records")

        else:
            log.warning("Failed to process the Interact message!")
        return False

    def sqs_interact_processing(self, event, dummy):
        """

        This function always return True to discard the message in all case.
        """
        event   = json.loads(event["body"])
        if "OpType" not in event:
            log.warning("Can't understand SQS message! (Missing 'OpType' required member of body dict)")
            return True

        command = event["OpType"].lower()
        if command not in self.commands.keys():
            log.warning("Received unknown command '%s' through SQS message!" % command)
        else:
            cmd = self.commands[command]
            if "sqs" not in cmd["interface"]:
                log.warn("Command '%s' not available through SQS queue!" % command)
                return True
            log.info("Loading prerequisites %s..." % cmd["prerequisites"])
            misc.load_prerequisites(self.context, cmd["prerequisites"])
            cmd["func"](self.context, event, {"headers":{}})
            log.info("Processed command '%s' through an SQS message." % command)
        return True
