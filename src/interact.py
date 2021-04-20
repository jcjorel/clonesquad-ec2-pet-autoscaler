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
import re
import kvtable
import traceback
import urllib.parse

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
        date = self.context["o_ec2"].get_state("cache.last_write_index", direct=True)
        if date is None or date != self.last_call_date:
            log.info(f"Invalidating cache ({date} != %s)." % self.last_call_date)
            self.cache          = {}
            self.last_call_date = date
            self.interact_precomputed_data = None
            self.invalidated    = True
            return True
        self.invalidated = False
        return False

    def is_invalidated(self):
        return self.invalidated

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
            #log.info("interact_data=%s" % interact_data)
            if interact_data is not None:
                log.log(log.NOTICE, "Loading Interact precomputed data...")
                self.interact_precomputed_data = misc.decode_json(interact_data)

    def save_cached_data(self, data):
        d = misc.encode_json(data, compress=True)
        self.context["o_state"].set_state("interact.precomputed", d, TTL=max(Cfg.get_duration_secs("app.run_period") * 2, 240))

# We set a global object that will survive multiple Lambda invocation to build a simple in-memory cache
query_cache = None

class Interact:
    def __init__(self, context):
        global query_cache
        self.context        = context
        if query_cache is None:
            query_cache = QueryCache(context)

        self.commands       = {
                "" : {
                    "interface": ["apigw"],
                    "cache": "none",
                    "clients": [],
                    "prerequisites": [],
                    "func": self.usage
                },
                "metadata"           : {
                    "interface": ["apigw"],
                    "cache": "client",
                    "clients": [],
                    "prerequisites": [],
                    "prepare": self.metadata_prepare,
                    "func": self.metadata,
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
                "configuration"       : {
                    "interface": ["apigw"],
                    "clients": ["dynamodb"],
                    "cache": "none",
                    "prerequisites": ["o_state"],
                    "func": self.configuration_dump,
                },
                "configuration/(.*)"       : {
                    "interface": ["apigw"],
                    "clients": ["dynamodb"],
                    "cache": "none",
                    "prerequisites": ["o_state"],
                    "func": self.configuration,
                },
                "scheduler"       : {
                    "interface": ["apigw"],
                    "clients": ["dynamodb"],
                    "cache": "none",
                    "prerequisites": [],
                    "func": self.scheduler_dump,
                },
                "scheduler/(.*)"       : {
                    "interface": ["apigw"],
                    "clients": ["dynamodb"],
                    "cache": "none",
                    "prerequisites": [],
                    "func": self.scheduler,
                },
                "debug/publishreportnow"   : {
                    "interface": ["sqs"],
                    "cache": "none",
                    "clients": ["ec2", "cloudwatch", "events", "sqs", "sns", "dynamodb", "elbv2", "rds", "resourcegroupstaggingapi", "s3"],
                    "prerequisites": ["o_state", "o_ec2", "o_notify", "o_targetgroup", "o_scheduler"],
                    "func": self.manage_publish_report,
                },
                "cloudwatch/metriccache": {
                    "interface": ["apigw"],
                    "clients": ["ec2", "cloudwatch"],
                    "cache": "global",
                    "prerequisites": ["o_state", "o_ec2", "o_notify", "o_cloudwatch"],
                    "func": self.cloudwatch_metric_cache
                },
                "cloudwatch/sentmetrics": {
                    "interface": ["apigw"],
                    "clients": [],
                    "cache": "global",
                    "prerequisites": [],
                    "prepare": self.cloudwatch_sentmetrics_prepare,
                    "func": self.cloudwatch_sentmetrics
                },
                "fleet/status": {
                    "interface": ["apigw"],
                    "cache": "global",
                    "clients": [],
                    "prerequisites": [],
                    "prepare": self.fleet_status_prepare,
                    "func": self.fleet_status
                },
                "fleet/metadata"           : {
                    "interface": ["apigw"],
                    "cache": "global",
                    "clients": [],
                    "prerequisites": [],
                    "func": self.allmetadatas,
                },
                "control/instances/(unstoppable|unstartable)" : {
                    "interface": ["apigw"],
                    "cache": "global",
                    "clients": [],
                    "prerequisites": ["o_state", "o_ec2"],
                    "func": self.control_instances,
                },
                "control/reschedulenow"  : {
                    "interface": ["apigw", "sqs"],
                    "cache": "global",
                    "clients": ["sqs"],
                    "prerequisites": ["o_state"],
                    "func": self.control_reschedulenow,
                },
                "backup"  : {
                    "interface": ["apigw", "sqs"],
                    "cache": "none",
                    "clients": [],
                    "prerequisites": ["o_state", "o_ec2", "o_scheduler"],
                    "func": self.backup,
                },
            }

    def get_prerequisites(self):
        return

    def backup(self, context, event, response, cacheddata):
        now        = self.context["now"]
        export_url = self.context["MetadataAndBackupS3Path"]
        if export_url is None or export_url == "":
            response["statusCode"] = 500
            response["body"]       = (f"Can not backup configuration and metadata! Please fillin 'MetadataAndBackupS3Path' CloudFormation "
                "parameter with a valid S3 bucket path!")
            return False

        o_ec2 = self.context["o_ec2"]
        # Export metadata and backup
        for d in [Cfg, o_ec2, self.context["o_scheduler"]]:
            d.exports_metadata_and_backup(export_url)

        # Export discovery metadata
        account_id, region, group_name = (self.context["ACCOUNT_ID"], self.context["AWS_DEFAULT_REGION"], self.context["GroupName"])
        path      = f"accountid={account_id}/region={region}/groupname={group_name}"
        discovery = misc.discovery(self.context, via_discovery_lambda=True)
        discovery["MetadataRecordLastUpdatedAtUTC"] = str(now).split("+")[0]
        discovery["Subfleets"]                   = o_ec2.get_subfleet_names()
        misc.put_url(f"{export_url}/metadata/discovery/{path}/{account_id}-{region}-discovery-cs-{group_name}.json", 
                json.dumps(discovery, default=str))

        response["statusCode"] = 200
        response["body"]       = f"Exported Configuration/Scheduler backups and metadata to {export_url}."
        log.info(response["body"])
        return False

    def control_instances(self, context, event, response, cacheddata):
        path = event["path"].split("/")[-1:][0]
        if path not in ["unstoppable", "unstartable"]:
            response["statusCode"] = 404
            response["body"]       = f"Unknown control state '{path}'!"
            return False

        filter_query = {}
        # The filter query can be sent by POST
        if "httpMethod" in event and event["httpMethod"] == "POST":
            try:
                filter_query = json.loads(event["body"])
            except Exception as e:
                response["statusCode"] = 400
                response["body"]       = f"Failed to parse JSON body: {e}!"
                return False

        # instance ids can be specified in the URL query string
        if event.get("instanceids"):
            filter_query["InstanceIds"] = event["instanceids"].split(",")
        # instance names can be specified in the URL query string
        if event.get("instancenames"):
            filter_query["InstanceNames"] = event["instancenames"].split(",")
        # Do we need to excluded the selected instance ids
        if event.get("excluded"):
            filter_query["Excluded"] = event.get("excluded") in ["true", "True"]
        # subfleet names can be specified in the URL query string
        if event.get("subfleetnames"):
            filter_query["SubfleetNames"] = event.get("subfleetnames").split(",") if event.get("subfleetnames") != "" else None

        mode = event.get("mode")
        if mode is not None:
            valid_modes = ["add", "delete"]
            if mode not in valid_modes:
                response["statusCode"] = 400
                response["body"]       = f"Invalid mode '{mode}'! (Must be one of {valid_modes})"
                return False
            self.context["o_ec2"].update_instance_control_state(path, mode, filter_query, event.get("ttl"))

        ctrl      = self.context["o_ec2"].get_instance_control_state()
        # Decorate the structure with current name of instance if any
        instances = self.context["o_ec2"].get_instances()
        for instance_id in ctrl[path].keys():
            instance      = next(filter(lambda i: i["InstanceId"] == instance_id, instances))
            name          = next(filter(lambda t: t["Key"] == "Name", instance["Tags"]), None)
            ctrl[path][instance_id]["InstanceName"] = name["Value"] if name is not None else None
            subfleet_name = next(filter(lambda t: t["Key"] == "clonesquad:subfleet-name", instance["Tags"]), None)
            ctrl[path][instance_id]["SubfleetName"] = subfleet_name["Value"] if subfleet_name is not None else None
        response["statusCode"] = 200
        response["body"]       = Dbg.pprint(ctrl[path])
        return False

    def control_reschedulenow(self, context, event, response, cacheddata):
        try:
            delay = int(event["delay"]) if "delay" in event else 0
            self.context["o_state"].set_state("main.last_call_date", "") # Remove the last execution date to allow immediate rescheduling
            sqs.call_me_back_send(delay=delay)
            response["statusCode"] = 200
            response["body"] = "On-demand rescheduling request acknowledged. Reschedule in %d second(s)..." % delay
        except Exception as e:
            response["statusCode"] = 400
            response["body"] = "Failed to parse supplied 'delay' parameter as a int() '%s' : %s" % (event["delay"], traceback.format_exc())
            log.exception(response["body"])
        return False

    def usage(self, context, event, response, cacheddata):
        response["statusCode"] = 200
        response["body"] =  "\n".join([ c for c in sorted(self.commands.keys()) if c != "" and "apigw" in self.commands[c]["interface"]])
        return True

    def manage_publish_report(self, context, event, response, cacheddata):
        return debug.manage_publish_report(context, event, response)

    def fleet_status_prepare(self):
        return {
            "EC2" : self.context["o_ec2_schedule"].get_synthetic_metrics(),
        }

    def fleet_status(self, context, event, response, cacheddata):
        response["statusCode"] = 200
        response["body"]       = Dbg.pprint(cacheddata)
        return True

    def cloudwatch_sentmetrics_prepare(self):
        return self.context["o_cloudwatch"].sent_metrics()

    def cloudwatch_sentmetrics(self, context, event, response, cacheddata):
        response["statusCode"] = 200
        response["body"]       = Dbg.pprint(cacheddata)
        return True

    def metadata_prepare(self):
        return self.context["o_ec2"].get_synthetic_metrics()

    def metadata(self, context, event, response, cacheddata):
        if "requestContext" not in event or "identity" not in event["requestContext"]:
            response["statusCode"] = 403
            response["body"]       = "Must call this API with AWS_IAM authentication."
            return False
        identity = event["requestContext"]["identity"]
        caller   = identity["caller"]
        try:
            access_key, instance_id = caller.split(":")
            if not instance_id.startswith("i-"):
                response["statusCode"] = 500
                response["body"]       = "Can't retrieve requesting InstanceId (%s)." % caller
                return False
        except:
            log.exception("Failed to retrieve IAM caller id (%s)!" % caller)
            response["statusCode"] = 500
            response["body"] = "Can't process metadata caller '%s'!" % caller
            return False

        if "instanceid" in event: instance_id = event["instanceid"] # Override from query string
        query_cache.load_cached_data()
        d        = next(filter(lambda d: d["InstanceId"] == instance_id, cacheddata), None)
        if d is None:
            response["statusCode"] = 400
            response["body"]       = "No information for instance id '%s'!" % instance_id
            return False
        cache = query_cache.get(f"metadata:ec2.instance.scaling.state.{instance_id}")
        if cache is not None:
            state = cache["body"]
        else:
            # Read ultra-fresh state of the instance directly from DynamodDB
            state = self.context["o_ec2"].get_state(f"ec2.instance.scaling.state.{instance_id}", direct=True)
        if state is not None and state != "":
            query_cache.put(f"metadata:ec2.instance.scaling.state.{instance_id}", {"body":state, "statusCode": 200})
            log.info(f"Read instance state for {instance_id} directly for state table ({state})")
            d["State"] = state
        response["statusCode"] = 200
        response["body"]       = Dbg.pprint(d)
        return True

    def allmetadatas(self, context, event, response, cacheddata):
        response["statusCode"] = 200
        query_cache.load_cached_data()
        response["body"]       = Dbg.pprint(query_cache.interact_precomputed_data["data"]["metadata"])
        return True

    def discovery(self, context, event, response, cacheddata):
        response["statusCode"]   = 200
        discovery = {
                "identity": event["requestContext"]["identity"],
                "discovery": misc.discovery(self.context, via_discovery_lambda=True)
            }
        response["body"]         = Dbg.pprint(discovery)
        return True

    def cloudwatch_metric_cache(self, context, event, response, cacheddata):
        response["statusCode"] = 200
        response["body"] = Dbg.pprint(context["o_cloudwatch"].get_metric_cache())
        return True

    def process_ack_event_dates(self, context, event, response, cacheddata):
        context["o_notify"].ack_event_dates(event["Events"])
        response["statusCode"] = 200
        response["body"]       = json.dumps({
            })
        return False

    def configuration_dump(self, context, event, response, cacheddata):
        response["statusCode"] = 200
        is_yaml                 = "format" in event and event["format"].lower() == "yaml"
        with_maintenance_window = "with_maintenance_window" in event and event["with_maintenance_window"].lower() == "true"
        if with_maintenance_window:
            # We load the EC2 and SSM modules to inherit their override parameters if a SSM Maintenance Window is active
            misc.load_prerequisites(self.context, ["o_ec2", "o_ssm"])

        if "httpMethod" in event and event["httpMethod"] == "POST":
            try:
                c = yaml.safe_load(event["body"]) if is_yaml else json.loads(event["body"])
                Cfg.import_dict(c)
                response["body"] = "Ok (%d key(s) processed)" % len(c.keys())
            except Exception as e:
                response["statusCode"] = 500
                response["body"] = "Can't parse YAML/JSON document : %s " % e
                return False
        else:
            only_stable_keys = "unstable" not in event or event["unstable"].lower() != "true"
            if "raw" in event and event["raw"].lower() == "true":
                dump = Cfg.get_dict() 
            else:
                dump = config.dumps(only_stable_keys=only_stable_keys) 
            response["body"] = yaml.dump(dump) if is_yaml else Dbg.pprint(dump)
        return True

    def configuration(self, context, event, response, cacheddata):
        m = re.search("configuration/(.*)$", event["OpType"])
        if m is None:
            response["statusCode"] = 400
            response["body"]       = "Missing config key path."
            return False
        config_key = m.group(1)
        with_maintenance_window = "with_maintenance_window" in event and event["with_maintenance_window"].lower() == "true"
        if with_maintenance_window:
            # We load the EC2 and SSM modules to inherit their override parameters if a SSM Maintenance Window is active
            misc.load_prerequisites(self.context, ["o_ec2", "o_ssm"])
        if "httpMethod" in event and event["httpMethod"] == "POST":
            value = event["body"].partition('\n')[0]
            log.info(f"TTL=%s" % event.get("ttl"))
            ttl   = misc.str2duration_seconds(event.get("ttl"), no_exception=True, default=None)
            log.info(f"Configuration write for key '{config_key}' = '{value}' (ttl={ttl}).")
            kvtable.KVTable.set_kv_direct(config_key, value, self.context["ConfigurationTable"], context=self.context, TTL=ttl)
            response["statusCode"] = 200
            response["body"] = value
        else:
            value = Cfg.get(config_key, none_on_failure=True)
            if value is None:
                response["statusCode"] = 400
                response["body"] = "Unknown configuration key '%s'!" % config_key
                return False
            else:
                response["statusCode"] = 200
                response["body"] = value
        return True

    def scheduler_dump(self, context, event, response, cacheddata):
        scheduler_table = kvtable.KVTable(self.context, self.context["SchedulerTable"])
        scheduler_table.reread_table()
        is_yaml = "format" in event and event["format"] == "yaml"
        response["statusCode"] = 200
        if "httpMethod" in event and event["httpMethod"] == "POST":
            try:
                c = yaml.safe_load(event["body"]) if is_yaml else json.loads(event["body"])
                scheduler_table.set_dict(c)
                response["body"] = "Ok (%d key(s) processed)" % len(c.keys())
            except Exception as e:
                response["statusCode"] = 500
                response["body"] = "Can't parse YAML/JSON document : %s " % e
                return False
        else:
            c = scheduler_table.get_dict()
            response["body"]     = yaml.dump(c) if is_yaml else Dbg.pprint(c)
        return True

    def scheduler(self, context, event, response, cacheddata):
        m = re.search("scheduler/(.*)$", event["OpType"])
        if m is None:
            response["statusCode"] = 400
            response["body"]       = "Missing config key path."
            return False
        config_key = m.group(1)
        if "httpMethod" in event and event["httpMethod"] == "POST":
            value = event["body"].partition('\n')[0]
            ttl   = misc.str2duration_seconds(event.get("ttl"), no_exception=True, default=None)
            log.info(f"Scheduler configuration write for key '{config_key}' = '{value}' (ttl={ttl}).")
            kvtable.KVTable.set_kv_direct(config_key, value, self.context["SchedulerTable"], context=self.context, TTL=ttl)
            response["statusCode"] = 200
            response["body"] = value
        else:
            value = kvtable.KVTable.get_kv_direct(config_key, self.context["SchedulerTable"], context=self.context)
            if value is None:
                response["statusCode"] = 400
                response["body"] = "Unknown configuration key!"
                return False
            else:
                response["statusCode"] = 200
                response["body"] = value
        return True

    def find_command(self, path):
        # Perfect match is prioritary
        if path in self.commands.keys():
            return self.commands[path]
        # Look up for regex command match.
        candidates = [ c for c in self.commands.keys() if ("*" in c or "(" in c) and re.match(c, path) ]
        return self.commands[candidates[0]] if len(candidates) else None

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
                querystring = "&".join([ "%s:%s" % (q, event["queryStringParameters"][q]) for q in event["queryStringParameters"].keys()])
                event.update(event["queryStringParameters"])

            log.log(log.NOTICE, "Received API Gateway message for path '%s'" % event["path"])

            # Normalize command format
            arg         = event["OpType"] if "OpType" in event else event["path"]
            path        = arg.lower().split("/")
            path_list   = list(filter(lambda x: x != "", path))
            command     = "/".join(path_list)
            command     = urllib.parse.unquote(command)
            cmd         = self.find_command(command)
            if cmd is None:
                response["statusCode"] = 404
                response["body"]       = "Unknown command '%s'" % (command)
                return True

            event["OpType"] = command
            #log.log(log.NOTICE, "Processing API Gateway command '%s'" % (command))
            if "apigw" not in cmd["interface"]:
                response["statusCode"] = 404
                response["body"]       = "Command not available through API Gateway"
                return True

            is_cacheable  = cmd["cache"] in ["global", "client"]
            if is_cacheable:
                cacheable_url = "%s?%s_%s" % (command, querystring, "" if cmd["cache"] == "global" else event["requestContext"]["identity"]["userArn"])
                #log.log(log.NOTICE, "Cacheable query '%s'" % cacheable_url)
                entry = query_cache.get(cacheable_url)
                if entry is not None:
                    response.update(entry)
                    log.log(log.NOTICE, "API Gateway query '%s' served from the cache..." % command)
                    return True
            misc.initialize_clients(cmd["clients"], self.context)
            misc.load_prerequisites(self.context, cmd["prerequisites"])
            cacheddata = None
            if "prepare" in cmd:
                #log.log(log.NOTICE, "Loading cached data...")
                query_cache.load_cached_data()
                if command not in query_cache.interact_precomputed_data["data"]:
                    response["statusCode"] = 500
                    response["body"]       = "Missing data"
                    return True
                cacheddata = query_cache.interact_precomputed_data["data"][command]
            if cmd["func"](self.context, event, response, cacheddata) and is_cacheable and response["statusCode"] == 200:
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
        command = urllib.parse.unquote(command)
        cmd     = self.find_command(command)
        if cmd is None:
            log.warning("Received unknown command '%s' through SQS message!" % command)
        else:
            cmd = self.commands[command]
            if "sqs" not in cmd["interface"]:
                log.warn("Command '%s' not available through SQS queue!" % command)
                return True
            cacheddata = None
            if "prepare" in cmd:
                log.log(log.NOTICE, "Loading cached data...")
                query_cache.load_cached_data()
                if command not in query_cache.interact_precomputed_data["data"]:
                    response["statusCode"] = 500
                    response["body"]       = "Missing data"
                    return True
                cacheddata = query_cache.interact_precomputed_data["data"][command]

            log.info("Loading prerequisites %s..." % cmd["prerequisites"])
            misc.initialize_clients(cmd["clients"], self.context)
            misc.load_prerequisites(self.context, cmd["prerequisites"])
            cmd["func"](self.context, event, {"headers":{}}, cacheddata)
            log.info("Processed command '%s' through an SQS message." % command)
        return True

    @xray_recorder.capture()
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

