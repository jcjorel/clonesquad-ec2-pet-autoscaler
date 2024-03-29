####
# HelperLib for CloudFormation Custom resource
#
from __future__ import print_function
import re
import pdb
import sys
import time
import json
import yaml
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
    data["PrivateDnsEnabled"] = False
    if "PrivateDnsEnabled" in edp:
        data["PrivateDnsEnabled"] = edp["PrivateDnsEnabled"].lower() == "true"
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

def get_apigw_vpc_endpoint(vpc_id, group_name, region):
    try:
        client   = boto3.client("ec2")
        response = client.describe_vpc_endpoints(Filters=[
            {
                'Name': 'vpc-id',
                'Values': [vpc_id]
            },
            {
                'Name': 'service-name',
                'Values': [f'com.amazonaws.{region}.execute-api']
            },
            {
                'Name': 'vpc-endpoint-type',
                'Values': ['Interface']
            }
            ])
    except Exception as e:
        raise ValueError(f"Failed to query VPC endpoints associated to VPC Id '{vpc_id}' : {e}")

    endpoints = [e for e in response["VpcEndpoints"] if e != "Available"]
    log.info(f"Detected available API Gateway endpoints: {endpoints}")

    # Select endpoints with a strategy that is open enough to serve the Interact API
    specific_endpoints = []
    generic_endpoints  = []
    for e in endpoints:
        tags    = e.get("Tags", [])
        if len([t for t in tags if t["Key"] == "clonesquad:preferred-vpc-endpoint" and t["Value"] == "*"]):
            specific_endpoints.append(e)
        if len([t for t in tags if t["Key"] == "clonesquad:preferred-vpc-endpoint" and t["Value"] == group_name]):
            generic_endpoints.append(e)

    if len(specific_endpoints) > 1:
        raise ValueError(f"Too much VPC endpoints tagged with 'clonesquad:preferred-vpc-endpoint' with value '{group_name}'")
    if len(specific_endpoints) == 1:
        log.info(f"Found a groupname specific endpoint {specific_endpoints[0]}.")
        return specific_endpoints[0]
    if len(generic_endpoints) > 1:
        raise ValueError(f"Too much VPC endpoints tagged with 'clonesquad:preferred-vpc-endpoint' with value '*'")
    if len(generic_endpoints) == 1:
        log.info(f"Found a generic endpoint {generic_endpoints[0]}.")
        return generic_endpoints[0]
    if len(endpoints) > 1:
        raise ValueError(f"Too much VPC endpoints without 'clonesquad:preferred-vpc-endpoint' tag.")
    if len(endpoints) == 1:
        log.info(f"As default, selected endpoint {endpoints[0]}.")
        return endpoints[0]
    raise ValueError(f"No suitable VPC API-GW/execute-api endpoint found!")

def ApiGWParameters_CreateOrUpdate(data, CloneSquadVersion=None, AccountId=None, Region=None, GroupName=None,
        ApiGWConfiguration=None, ApiGWEndpointConfiguration=None, DefaultGWPolicyURL=None):
    data["GWType"]         = "REGIONAL"
    data["GWPolicy"]       = get_policy_content(DefaultGWPolicyURL, AccountId, Region)
    data["VpcEndpointDNS"] = ""
    config                 = []
    if ApiGWConfiguration != "None":
        config           = misc.parse_line_as_list_of_dict(ApiGWConfiguration, leading_keyname="GWType")
        if len(config): data.update(config[0])

    CONFIG_KEYS = ["GWPolicy", "GWType", "VpcEndpointDNS", "VpcId"]
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
            if kw == "VpcId":
                endpoint = get_apigw_vpc_endpoint(a[kw], GroupName, Region)
                if endpoint:
                    data["VpcEndpointDNS"] = endpoint["DnsEntries"][0]["DnsName"]

    if len(ApiGWEndpointConfiguration) == 0 and data["VpcEndpointDNS"] in ["", "None"]:
        log.warning("When no 'ApiGWEndpointConfiguration' is defined, you should either specify 'VpcId' or "
                "'VpcEndpointDNS' keywords in 'ApiGWParameters' to populate the 'ApiGwVpcEndpointDNSEntry' field of "
                "the discovery API/Function.")
        data["VpcEndpointDNS"] = ""

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
        GroupName=None, LoggingS3Path=None, MetadataAndBackupS3Path=None, InteractSQSQueueIAMPolicySpec=None):
    data["InstallTime"] = str(misc.utc_now())

    data["InteractSQSQueueIAMPolicy.Principal"] = {
            "AWS": AccountId
        }
    data["InteractSQSQueueIAMPolicy.Condition"] = {}
    if InteractSQSQueueIAMPolicySpec not in [None, "None"]:
        try:
            policy = json.loads(InteractSQSQueueIAMPolicySpec)
            if "Principal" in policy:
                data["InteractSQSQueueIAMPolicy.Principal"] = policy["Principal"]
            if "Condition" in policy:
                data["InteractSQSQueueIAMPolicy.Condition"] = policy["Condition"]
        except Exception as e:
            raise ValueError(f"Failed to parse 'InteractSQSQueueIAMPolicy' as JSON document.")
        log.info(f'SQS policy modified with user supplied policy snippet: {policy}')

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
        return (None, None)

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
        if MetadataAndBackupS3Path not in ["", "None", None]:
            logging_bucket_name = f"clonesquad-metada-and-backup-s3-path-bucket-name-{AccountId}-{Region}"
            logging_key_name    = authpath
            fullpath            = f"{MetadataAndBackupS3Path}/{authpath}/accountid={AccountId}/region={Region}/groupname={GroupName}"
            logging_bucket_name, logging_key_name = _check_and_format_s3_path("MetadataAndBackupS3Path", fullpath)
        else:
            logging_bucket_name, logging_key_name = (f"not-configured-{AccountId}-{Region}", authpath)
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
    try:
        function(helper.Data, **parameters)
    except Exception as e:
        log.exception(f"Got Exception in {function_name}!")
        raise e
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

