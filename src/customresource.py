####
# HelperLib for CloudFormation Custom resource
#
from __future__ import print_function
import re
import pdb
import sys
import time
import json
import boto3
from crhelper import CfnResource
import logging

import cslog
log = cslog.logger(__name__)

import ipaddress
from aws_xray_sdk import global_sdk_config
global_sdk_config.set_sdk_enabled(False) # Disable xray
import config as Cfg
import debug as Dbg
import misc

# Initialise the helper, all inputs are optional, this example shows the defaults
helper = CfnResource(json_logging=True, log_level='DEBUG', boto_level='CRITICAL', sleep_on_delete=120)

try:
    ## Init code goes here
    pass
except Exception as e:
    helper.init_failure(e)


def get_policy_content(url, account_id, region, api_gw_id=None):
    content = misc.get_url(url)
    if content is None:
        raise ValueError("Failed to load specified GWPolicyUrl '%s'!" % url)
    log.info(f"Success for get URL {url} : %s" % str(content, "utf-8"))
    try:
        content = str(content, "utf-8").replace("%(AccountId)", account_id).replace("%(Region)", region)
        if api_gw_id is not None:
            content = content.replace("%(ApiGWId)", api_gw_id)
        return json.loads(content)
    except:
        log.exception("Failed to parse the API Gateway policy located at '%s'!" % url)

def generate_igress_sg_rule(trusted_clients):
    rule = []
    for client in trusted_clients.split(","):
        if client == "":
            continue
        sg_spec  = {
            "FromPort": "443",
            "ToPort": "443",
            "IpProtocol": "TCP"
            }
        if client.startswith("sg-"):
            if ":" in client:
                sg, ownerid = client.split(":")
                sg_spec.update({
                    "SourceSecurityGroupOwnerId": ownerid,
                    "SourceSecurityGroupId": sg
                    })
            else:
                sg_spec.update({
                    "SourceSecurityGroupId": client
                    })
        elif client.startswith("pl-"):
            sg_spec.update({
                "SourcePrefixListId": client
                })
        else:
            try:
                # Ensure that we have a well-formed IP networks
                client = str(ipaddress.IPv4Network(client))
                sg_spec.update({
                    "CidrIp": client
                    })
            except:
                raise ValueError("Failed to interpret '%s' as an IPv4 CIDR network!" % client)
        rule.append(sg_spec)
    return rule
    

def ApiGWVpcEndpointParameters_CreateOrUpdate(data, CloneSquadVersion=None, AccountId=None, Region=None, ApiGWId=None,
        ApiGWConfiguration=None, ApiGWEndpointConfiguration=None, DefaultGWVpcEndpointPolicyURL=None):

    endpoint_config = misc.parse_line_as_list_of_dict(ApiGWEndpointConfiguration, with_leading_string=False)
    edp             = endpoint_config[0]
    if len(endpoint_config): data.update(edp)

    if "VpcId" not in edp:
        raise ValueError("'VpcId' keyword is mandatory for ApiGWEndpointConfiguration!")
    data["VpcId"] = edp["VpcId"]
    del edp["VpcId"]

    # Policy Document
    data["PolicyDocument"] = get_policy_content(DefaultGWVpcEndpointPolicyURL, AccountId, Region, api_gw_id=ApiGWId)
    if "VpcEndpointPolicyURL" in edp:
        data["PolicyDocument"] = get_policy_content(edp["VpcEndpointPolicyURL"], AccountId, Region, api_gw_id=ApiGWId)

    # Get SubnetIds list
    if "SubnetIds" in edp:
        subnet_ids = edp["SubnetIds"].split(",")
        del edp["SubnetIds"]
    else:
        # Fetch all Subnets of the VPC
        client   = boto3.client("ec2")
        response = client.describe_subnets(
            Filters=[
               {"Name": "vpc-id",
               "Values": [ data["VpcId"] ]}
               ]
            )
        if not len(response["Subnets"]):
            raise ValueError("Specified VPC '%s' doesn't contain any subnet!" % data["VpcId"])
        subnet_ids = [s["SubnetId"] for s in response["Subnets"]]
    log.info("SubnetIds=%s" % subnet_ids)
    data["SubnetIds"] = subnet_ids

    # PrivateDnsEnabled
    data["PrivateDnsEnabled"] = True
    if "PrivateDnsEnabled" in edp:
        data["PrivateDnsEnabled"] = bool(edp["PrivateDnsEnabled"])
        del edp["PrivateDnsEnabled"]

    # Security group for VPC Endpoint
    data["SecurityGroupIngressRule"] = [{
        "IpProtocol": "-1",
        "FromPort": "-1",
        "ToPort": "-1",
        "CidrIp": "0.0.0.0/0"
        }]
    if "TrustedClients" in edp:
        data["SecurityGroupIngressRule"] = generate_igress_sg_rule(edp["TrustedClients"])
        del edp["TrustedClients"]

    if len(edp.keys()):
        raise ValueError("Unknown keywords in ApiGWVpcEndpointParameters '%s'!" % edp.keys())


def ApiGWParameters_CreateOrUpdate(data, CloneSquadVersion=None, AccountId=None, Region=None, Dummy=None,
        ApiGWConfiguration=None, ApiGWEndpointConfiguration=None, DefaultGWPolicyURL=None):
    data["GWType"]   = "REGIONAL"
    data["GWPolicy"] = get_policy_content(DefaultGWPolicyURL, AccountId, Region)
    data["VpcEndpointDNS"] = ""
    config           = misc.parse_line_as_list_of_dict(ApiGWConfiguration, leading_keyname="GWType")
    if len(config): data.update(config[0])

    CONFIG_KEYS = ["GWPolicy", "GWType", "VpcEndpointDNS"]
    if len(config):
        a = config[0]
        for kw in a.keys():
            if kw not in CONFIG_KEYS:
                raise ValueError("ApiGWConfiguration: Unknown meta key '%s'!" % kw)
            if kw == "GWType":
                valid_endpoint_configurations = ["REGIONAL", "PRIVATE"]
                if a[kw] not in valid_endpoint_configurations:
                    raise ValueError("Can't set API GW Endpoint to value '%s'! (valid values are %s)" % (a[kw], valid_endpoint_configurations))
            if kw == "GWPolicy" and len(a[kw]):
                data["GWPolicy"] = get_policy_content(a[kw], AccountId, Region)

    log.info(Dbg.pprint(data["GWPolicy"]))
    data["EndpointConfiguration.Type"] = data["GWType"]

def DynamoDBParameters_CreateOrUpdate(data, CloneSquadVersion=None, AccountId=None, Region=None, 
        DynamoDBConfiguration=None):

    config = misc.parse_line_as_list_of_dict(DynamoDBConfiguration, with_leading_string=False)

    TABLES = ["ConfigTable", "AlarmStateEC2Table", "EventTable", "LongTermEventTable", "SchedulerTable", "StateTable"]
    for c in TABLES:
        data["%s.BillingMode" % c]           = "PAY_PER_REQUEST"
        data["%s.ProvisionedThroughput" % c] = { 
                "ReadCapacityUnits" : "0",
                "WriteCapacityUnits": "0"
            }
        if len(config) and c in config[0] and config[0][c] != "":
            try:
                capacity                      = config[0][c].split(":")
                read_capacity, write_capacity = (capacity[0], capacity[1] if len(capacity) > 1 else capacity[0])
                data["%s.BillingMode" % c]           = "PROVISIONED"
                data["%s.ProvisionedThroughput" % c] = {
                        "ReadCapacityUnits" : str(int(read_capacity)),
                        "WriteCapacityUnits": str(int(write_capacity))
                    }
            except Exception as e:
                raise ValueError("Failed to parse DynamoDBParameters keyword '%s' with value '%s'!" % (c, config[0][c]))

def GeneralParameters_CreateOrUpdate(data, CloneSquadVersion=None, AccountId=None, Region=None, 
        GroupName=None, LoggingS3Path=None, MetadataAndBackupS3Path=None, InteractSQSQueueIAMPolicyCondition=None):
    data["InstallTime"] = str(misc.utc_now())

    if InteractSQSQueueIAMPolicyCondition not in [None, "None"]:
        try:
            data["InteractSQSQueueIAMPolicyCondition"] = json.loads(InteractSQSQueueIAMPolicyCondition)
        except Exception as e:
            raise ValueError(f"Failed to parse 'InteractSQSQueueIAMPolicyCondition' as JSON document.")

    def _check_and_format_s3_path(envname, url):
        if url.startswith("s3://"):
            path  = url[5:]
            parts = path.split("/", 1)
            logging_bucket_name = parts[0]
            logging_key_name    = parts[1] if len(parts) > 1 else ""
            logging_key_name    = "/".join([s for s in logging_key_name.split("/") if s != ""]) # Remove extra slashes
            if logging_key_name == "":
                logging_key_name = "*"
            else:
                if not logging_key_name.endswith("/") and not logging_key_name.endswith("*"):
                    logging_key_name += "/*"
                if logging_key_name.endswith("/"):
                    logging_key_name += "*"
            return (logging_bucket_name, logging_key_name)
        elif url not in ["", "None"] and len(url):
            raise ValueError(f"{envname} must start with s3://! : {url}")

    # Manage LoggingS3Path
    logging_bucket_name = f"clonesquad-logging-s3-path-bucket-name-{AccountId}-{Region}"
    logging_key_name    = "is-not-configured"
    logging_bucket_name, logging_key_name = _check_and_format_s3_path("LoggingS3Path", LoggingS3Path)
    data["LoggingS3PathArn"] = f"arn:aws:s3:::{logging_bucket_name}/{logging_key_name}"

    # Manage MetadataAndBackupS3Path
    authorized_paths = [
        {
            "VarName": "Backup",
            "Path": "backups"
        },
        {
            "VarName": "MetadataConfiguration",
            "Path": "metadata/configuration"
        },
        {
            "VarName": "MetadataScheduler",
            "Path": "metadata/scheduler"
        },
        {
            "VarName": "MetadataDiscovery",
            "Path": "metadata/discovery"
        },
        {
            "VarName": "MetadataInstances",
            "Path": "metadata/instances"
        },
        {
            "VarName": "MetadataVolumes",
            "Path": "metadata/volumes"
        },
        {
            "VarName": "MetadataMaintenanceWindows",
            "Path": "metadata/maintenance-windows"
        },
    ]
    for p in authorized_paths:
        varname, authpath   = (p["VarName"], p["Path"])
        logging_bucket_name = f"clonesquad-metada-and-backup-s3-path-bucket-name-{AccountId}-{Region}"
        logging_key_name    = authpath
        fullpath            = f"{MetadataAndBackupS3Path}/{authpath}/accountid={AccountId}/region={Region}/groupname={GroupName}"
        logging_bucket_name, logging_key_name = _check_and_format_s3_path("MetadataAndBackupS3Path", fullpath)
        data[f"{varname}S3PathArn"] = f"arn:aws:s3:::{logging_bucket_name}/{logging_key_name}"

def call(event, context):
    parameters    = event["ResourceProperties"].copy()
    request_type  = event["RequestType"]
    function_name = "%s_%s" % (parameters["Helper"], request_type)
    match_name    = "%s_.*%s.*" % (parameters["Helper"], request_type)
    if "Helper" not in parameters:
        raise ValueError("Missing 'Helper' resource property!")
    if function_name not in globals():
        function_name = next(filter(lambda f: re.match(match_name, f), globals()), None)
        if function_name is None:
            raise ValueError("Unknown helper function '%s'!" % function_name)
    del parameters["Helper"]
    del parameters["ServiceToken"]
    log.debug(Dbg.pprint(parameters))

    log.info("Calling helper function '%s'(%s)..." % (function_name, parameters))
    function       = globals()[function_name]
    function(helper.Data, **parameters)
    log.info("Data: %s" % helper.Data)
    print("Data: %s" % Dbg.pprint(helper.Data))

@helper.create
def create(event, context):
    call(event, context)

@helper.update
def update(event, context):
    call(event, context)

@helper.delete
def delete(event, context):
    return

def handler(event, context):
    helper(event, context)


class ContextMock():
    def __init__(self):
        self.aws_request_id = "12345"

    def get_remaining_time_in_millis(self):
        return 15 * 60 * 1000

    def _send(self, status=None, reason="", send_response=None):
        log.info("Sending response... (fake)")

if __name__ == '__main__':
    _is_local_test = True
    context = ContextMock()
    helper._send = context._send

    # For test purpose
    event = {
            "RequestType": "Create",
            "StackId": "arn:aws:cloudformation:eu-west-1:111111111111:stack/MyTesStack/9c08b090-0a87-11eb-9f09-021e20b443de",
            "RequestId": "MyRequestId",
            "LogicalResourceId": "MyLogicalResourceId",
            "ResponseURL": "https://somewhere",
            "ResourceProperties" : {
                "ServiceToken": "DummyToken",
                "Helper": "ApiGWParameters",
                "ApiGWConfiguration": "PRIVATE", #,GWPolicy=https://www.w3schools.com/",
                "ApiGWEndpointConfiguration": "VpcId=vpc-1235",
                "DefaultGWPolicyURL": "internal:api-gw-default-policy.json",
                "AccountId": "111111111111",
                "Region": "eu-west-3"
            }
    }
    handler(event, context)
    event = {
            "RequestType": "Create",
            "StackId": "arn:aws:cloudformation:eu-west-1:111111111111:stack/MyTesStack/9c08b090-0a87-11eb-9f09-021e20b443de",
            "RequestId": "MyRequestId",
            "LogicalResourceId": "MyLogicalResourceId",
            "ResponseURL": "https://somewhere",
            "ResourceProperties" : {
                "ServiceToken": "DummyToken",
                "Helper": "ApiGWVpcEndpointParameters",
                "ApiGWEndpointConfiguration": "VpcId=vpc-e119f098,PrivateDnsEnabled=True,TrustedClients=10.0.0.0/10\\,sg-azererfzer",
                "DefaultGWVpcEndpointPolicyURL": "internal:api-gw-default-endpoint-policy.json",
                "ApiGWId": "gw-sdfdfsd",
                "AccountId": "111111111111",
                "Region": "eu-west-3"
            }
    }
    handler(event, context)

