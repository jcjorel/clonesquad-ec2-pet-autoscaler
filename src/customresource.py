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

def get_policy_content(url, account_id):
    content = misc.get_url(url)
    if content is None:
        raise ValueError("Failed to load specified GWPolicyUrl '%s'!" % url)
    return str(content, "utf-8").replace("%(AccountId)", account_id)

def ApiGWParameters_CreateOrUpdate(data, AccountId=None, Region=None, 
        ApiGWConfiguration=None, ApiGWEndpointConfiguration=None, DefaultGWPolicyURL=None):
    data["GWType"]   = "REGIONAL"
    data["GWPolicy"] = get_policy_content(DefaultGWPolicyURL, AccountId)
    config           = misc.parse_line_as_list_of_dict(ApiGWConfiguration, leading_keyname="GWType")
    if len(config): data.update(config[0])

    endpoint_config  = misc.parse_line_as_list_of_dict(ApiGWEndpointConfiguration, with_leading_string=False)
    if len(endpoint_config): data.update(endpoint_config[0])

    CONFIG_KEYS = ["GWPolicy", "GWType"]
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
                data["GWPolicy"] = get_policy_content(a[kw], AccountId)

    try:
        data["GWPolicy"] = json.loads(data["GWPolicy"])
    except:
        log.exception("Failed to parse the API Gateway policy!")
    log.info(Dbg.pprint(data["GWPolicy"]))
    data["EndpointConfiguration.Type"] = data["GWType"]

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
                "AccountId": "111111111111"
            }
    }
    handler(event, context)
