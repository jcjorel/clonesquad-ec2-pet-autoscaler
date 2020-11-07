import os
import json
import debug
import debug as Dbg
import config
import sqs

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class Interact:
    def __init__(self, context):
        self.context = context
        self.commands = {
                "APIGateway/Url"           : self.get_api_url,
                "Notify/AckEvent"          : self.process_ack_event_dates,
                "Configuration/Dump"       : self.dump_configuration,
                "Debug/PublishReportNow"   : debug.manage_publish_report,
                "Cloudwatch/GetMetricCache": self.cloudwatch_get_metric_cache
                }

    def get_prerequisites(self):
        return

    def get_api_url(self, context, event, response):
        response["statusCode"]   = 200
        response["body"]         = contex["InteractAPIGatewayUrl"]
        response["Content-Type"] = "test/plain"

    def cloudwatch_get_metric_cache(self, context, event, response):
        response["statusCode"] = 200
        response["body"] = Dbg.pprint(context["o_cloudwatch"].get_metric_cache())

    def process_ack_event_dates(self, context, event, response):
        context["o_notify"].ack_event_dates(event["Events"])
        response["statusCode"] = 200
        response["body"]       = json.dumps({
            })

    def dump_configuration(self, context, event, response):
        response["statusCode"] = 200
        only_stable_keys       = "Unstable" not in event or event["Unstable"] != "True"
        response["body"]       = Dbg.pprint(config.dumps(only_stable_keys=only_stable_keys))

    def handler(self, event, context, response):
        if "httpMethod" in event and "path" in event:
            response.update({
                  "isBase64Encoded" : False,
                  "statusCode": 500,
                  "headers": {
                      "Content-Type": "application/json"
                  },
                  "body": ""
                  })

            if "queryStringParameters" in event and event["queryStringParameters"] is not None:
                event.update(event["queryStringParameters"])

            if event["httpMethod"] == "POST":
                try:
                    b = json.loads(event["body"])
                    event.update(b)
                except Exception as e:
                    log.exception("Failed to handle POST body! : %s" % event["body"])
                    raise e

            log.log(log.NOTICE, "Received API Gateway message for path '%s'" % event["path"])

            # Normalize command format
            arg         = event["OpType"] if "OpType" in event else event["path"]
            path        = arg.split("/")
            path_list   = list(filter(lambda x: x != "", path))
            command     = "/".join(path_list)
            if command not in self.commands.keys():
                response["statusCode"] = 404
                response["body"]       = "Unknown command '%s'" % (command)
                return True

            event["OpType"] = command
            log.log(log.NOTICE, "Processing API Gateway command '%s'" % (command))
            self.commands[command](self.context, event, response)
            return True

        elif self.context["o_scheduler"].manage_rule_event(event):
            log.log(log.NOTICE, "Processed Cloudwatch Scheduler event")
            # Cloudwatch Scheduler event

        elif sqs.process_sqs_records(event, function=self.sqs_interact_processing):
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

        command = event["OpType"]
        if command not in self.commands.keys():
            log.warning("Received unknown command '%s' through SQS message!")
        else:
            self.commands[command](self.context, event, {"headers":{}})
            log.info("Processed command '%s' through an SQS message." % command)
        return True
