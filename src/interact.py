import os
import json
import yaml
import debug
import debug as Dbg
import config
import sqs
import misc
import config as Cfg
import pdb

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class QueryCache:

    def __init__(self, context):
        self.context = context
        self.cache   = {}
        self.interact_precomputed_data = None
        self.last_call_date = None
        self.invalidated = False

    def check_invalidation(self):
        date = self.context["o_ec2"].get_state("main.last_call_date", direct=True)
        if date != self.last_call_date:
            self.cache          = {}
            self.last_call_date = date
            self.load_cached    = True
        else:
            self.load_cached    = False

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

    def load_cached_data(self):
        if self.interact_precomputed_data is None:
            interact_data = self.context["o_state"].get_state("interact.precomputed", direct=True)
            log.info("interact_data=%s" % interact_data)
            if interact_data is not None:
                log.log(log.NOTICE, "Loading Interact precomputed data...")
                self.interact_precomputed_data = misc.decode_json(interact_data)

    def save_cached_data(self, data):
        d = misc.encode_json(data, compress=True)
        self.context["o_state"].set_state("interact.precomputed", d, TTL=(Cfg.get_duration_secs("app.run_period") * 2))

# We set a global object that will survive multiple Lambda invocation to build a simple in-memory cache
query_cache = None

class Interact:
    def __init__(self, context):
        global query_cache
        self.context        = context
        if query_cache is None:
            query_cache = QueryCache(context)

        self.commands       = {
                "metadata"           : {
                    "interface": ["apigw"],
                    "cache": "client",
                    "clients": [],
                    "prerequisites": [],
                    "prepare": self.metadata_prepare,
                    "func": self.metadata,
                },
                "metadatas"           : {
                    "interface": ["apigw"],
                    "cache": "global",
                    "clients": [],
                    "prerequisites": [],
                    "func": self.metadatas,
                },
                "discovery"           : {
                    "interface": ["apigw"],
                    "cache": "none",
                    "clients": [],
                    "prerequisites": [],
                    "func": self.discovery,
                },
                "notify/ackevent"          : {
                    "interface": ["sqs", "apigw"],
                    "cache": "none",
                    "clients": ["dynamodb"],
                    "prerequisites": [], 
                    "func": self.process_ack_event_dates,
                },
                "configuration/json"       : {
                    "cache": "none",
                    "interface": ["apigw"],
                    "clients": ["dynamodb"],
                    "cache": "none",
                    "prerequisites": [],
                    "func": self.configuration_json,
                },
                "configuration/yaml"       : {
                    "cache": "none",
                    "interface": ["apigw"],
                    "clients": ["dynamodb"],
                    "cache": "none",
                    "prerequisites": [],
                    "func": self.configuration_yaml,
                },
                "debug/publishreportnow"   : {
                    "interface": ["sqs"],
                    "cache": "none",
                    "clients": ["ec2"],
                    "prerequisites": ["o_state", "o_ec2", "o_notify", "o_targetgroup", "o_scheduler"],
                    "func": debug.manage_publish_report,
                },
                "cloudwatch/getmetriccache": {
                    "interface": ["apigw"],
                    "cache": "global",
                    "clients": ["ec2", "cloudwatch"],
                    "cache": "global",
                    "prerequisites": ["o_state", "o_ec2", "o_notify", "o_cloudwatch"],
                    "func": self.cloudwatch_get_metric_cache
                }
            }

    def get_prerequisites(self):
        return

    def metadata_prepare(self):
        data = {}
        ec2  = self.context["o_ec2"]
        az_with_issues = ec2.get_azs_with_issues()
        for i in self.context["o_ec2"].get_instances(State="pending,running"):
            instance_id    = i["InstanceId"]
            instance_state = ec2.get_scaling_state(instance_id)
            data[instance_id] = {
                "AZWithIssues" : az_with_issues,
                "Instance": {
                    "Tags"     : i["Tags"],
                    "Status"   : [state for state in ec2.INSTANCE_STATES if ec2.is_instance_state(instance_id, [state])][0],
                    "State"    : instance_state if instance_state is not None else i["State"]["Name"]
                }
            }
        return data

    def metadata(self, context, event, response):
        if "metadata" not in query_cache.interact_precomputed_data["data"]:
            response["statusCode"] = 500
            response["body"]       = "Empty cache"
            return True
        data = query_cache.interact_precomputed_data["data"]["metadata"]
        if "requestContext" not in event or "identity" not in event["requestContext"]:
            response["statusCode"] = 403
            response["body"]       = "Must call this API with AWS_IAM authentication."
            return True
        identity = event["requestContext"]["identity"]
        caller   = identity["caller"]
        try:
            access_key, instance_id = caller.split(":")
            if not instance_id.startswith("i-"):
                response["statusCode"] = 500
                response["body"]       = "Can't retrieve requesting InstanceId (%s)." % caller
                return True
        except:
            log.exception("Failed to retrieve IAM caller id (%s)!" % caller)

        if instance_id not in data:
            response["statusCode"] = 400
            response["body"]       = "No information for instance id '%s'!" % instance_id
            return True

        response["statusCode"] = 200
        response["body"]       = Dbg.pprint(data[instance_id])
        return True

    def metadatas(self, context, event, response):
        response["statusCode"] = 200
        query_cache.load_cached_data()
        response["body"]       = Dbg.pprint(query_cache.interact_precomputed_data["data"]["metadata"])
        return True

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

    def configuration_json(self, context, event, response):
        response["statusCode"] = 200
        only_stable_keys       = "Unstable" not in event or event["Unstable"] != "True"
        response["body"]       = Dbg.pprint(config.dumps(only_stable_keys=only_stable_keys))
        return True

    def configuration_yaml(self, context, event, response):
        response["statusCode"] = 200
        if "httpMethod" in event and event["httpMethod"] == "POST":
            try:
                c = yaml.safe_load(event["body"])
                Cfg.import_dict(c)
                response["body"] = "Ok"
            except Exception as e:
                response["statusCode"] = 500
                response["body"] = "Can't parse YAML document : %s " % e
        else:
            only_stable_keys       = "Unstable" not in event or event["Unstable"] != "True"
            response["body"]       = yaml.dump(config.dumps(only_stable_keys=only_stable_keys))
        return True

    def handler(self, event, context, response):
        global query_cache

        query_cache.check_invalidation()

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
                log.log(log.NOTICE, "Cacheable query '%s'" % cacheable_url)
                entry = query_cache.get(cacheable_url)
                if entry is not None:
                    response.update(entry)
                    log.log(log.NOTICE, "API Gateway query '%s' served from the cache..." % command)
                    return True
            misc.initialize_clients(cmd["clients"], self.context)
            misc.load_prerequisites(self.context, cmd["prerequisites"])
            if "prepare" in cmd:
                log.log(log.NOTICE, "Loading cached data...")
                query_cache.load_cached_data()
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
            if "prepare" in cmd:
                log.log(log.NOTICE, "Loading cached data...")
                query_cache.load_cached_data()

            log.info("Loading prerequisites %s..." % cmd["prerequisites"])
            misc.initialize_clients(cmd["clients"], self.context)
            misc.load_prerequisites(self.context, cmd["prerequisites"])
            cmd["func"](self.context, event, {"headers":{}})
            log.info("Processed command '%s' through an SQS message." % command)
        return True

    def pregenerate_interact_data(self):
        """ In order to keep the API Gateway fast, we pre-compute some data during the
            the processing of the Main Lambda function.
        """
        interact_precomputed_data = {
                "data": {}
        }
        for api in self.commands.keys():
            cmd = self.commands[api]
            if "prepare" in cmd:
                interact_precomputed_data["data"][api] = cmd["prepare"]()
        query_cache.save_cached_data(interact_precomputed_data)
