import os
import sys
import re
import hashlib
import json
import yaml
import math

import gzip
# Hack: Force gzip to have a deterministic output (See https://stackoverflow.com/questions/264224/setting-the-gzip-timestamp-from-python/264303#264303)
class GzipFakeTime:
   def time(self):
      return 1.1
gzip.time = GzipFakeTime() 

import base64
import boto3
from botocore.config import Config
from datetime import datetime
from datetime import timezone
from datetime import timedelta
import dateutil
import requests
from requests_file import FileAdapter
from collections import defaultdict
from iamauth import IAMAuth
import pdb
import debug as Dbg

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()


def is_sam_local():
    return "AWS_SAM_LOCAL" in os.environ and os.environ["AWS_SAM_LOCAL"] == "true"

import cslog
log = cslog.logger(__name__)


class Boto3ProxyClass(object):
    """ boto3 proxy class.
    
    Used for debugging purpose yet.

    """
    _clients   = {}
    _responses = []
    def __init__(self, client):
        object.__setattr__( self, "_client", client)

    @staticmethod
    def client(cl, config=None):
        if cl in Boto3ProxyClass._clients:
            return Boto3ProxyClass._clients[cl]
        Boto3ProxyClass._clients[cl] = Boto3ProxyClass(boto3.client(cl, config=config))
        return Boto3ProxyClass._clients[cl]

    def _proxy_call(self, fname, f, *args, **kwargs):
        responses = object.__getattribute__(self, "_responses")
        frame = {
            "call": fname,
            "args": args,
            "kwargs": kwargs,
        }
        r = f(*args, **kwargs)
        if fname.startswith("describe"):
            frame["response"] = r
            #responses.append(frame) # Uncomment this to record all describe API call responses
        return r

    def __getattribute__(self, name):
        attr = getattr(object.__getattribute__(self, "_client"), name)
        if hasattr(attr, '__call__'):
            return lambda *args, **kwargs: object.__getattribute__(self, "_proxy_call")(name, attr, *args, **kwargs)
        return attr

    def __delattr__(self, name):
        delattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_client"), name, value)

    def __nonzero__(self):
        return bool(object.__getattribute__(self, "_client"))

    def __str__(self):
        return str(object.__getattribute__(self, "_client"))

    def __repr__(self):
        return repr(object.__getattribute__(self, "_client"))

    def __hash__(self):
        return hash(object.__getattribute__(self, "_client"))


def is_direct_launch():
    return len(sys.argv) > 1

def utc_now():
    return datetime.now(tz=timezone.utc) # datetime.utcnow()

def local_now(ctx):
    timezones  = yaml.safe_load(get_url("internal:region-timezones.yaml"))
    tz         = os.getenv("TimeZone") 
    tz         = timezones.get(ctx["AWS_DEFAULT_REGION"]) if (tz is None or tz == "") else tz
    tz         = tz if tz else "UTC"
    return datetime.now(tz=dateutil.tz.gettz(tz))

def athena_timestamp_format(date):
    """ Return a timestamp with timezone
    """
    date_str = str(date).split("+")[0]
    return f"{date_str} %s" % date.strftime("%Z")

def stringify_timestamps(struct, utc_naive=True, athena_timestamp=False):
    """ This function walkthough the 'struct' and stringify all datetime objects encountered
        either with standard date + timezone format, UTC naive or an AWS Athena compatible one with TZ.
    """
    if isinstance(struct, dict):
        for k in struct:
            if isinstance(struct[k], dict) or isinstance(struct[k], list):
                stringify_timestamps(struct[k], athena_timestamp=athena_timestamp)
            elif isinstance(struct[k], datetime):
                if utc_naive:
                    struct[k] = str(struct[k]).split("+")[0]
                elif athena_timestamp_format:
                    struct[k] = str(struct[k])
                else:
                    struct[k] = athena_timestamp_format(struct[k])
    if isinstance(struct, list):
        for i in range(0, len(struct)):
            if isinstance(struct[i], dict) or isinstance(struct[i], list):
                stringify_timestamps(struct[i], athena_timestamp=athena_timestamp)
            if isinstance(struct[i], datetime):
                if utc_naive:
                    struct[i] = str(struct[i]).split("+")[0]
                elif athena_timestamp_format:
                    struct[i] = str(struct[i])
                else:
                    struct[i] = athena_timestamp_format(struct[i])
    return struct

def epoch():
    return seconds2utc(0)

def seconds_from_epoch_utc(now=None):
    if now is None: now = utc_now()
    return int((now - epoch()).total_seconds())

def seconds2utc(seconds):
    return datetime.utcfromtimestamp(int(seconds)).replace(tzinfo=timezone.utc)

def str2utc(s, default=None):
    if isinstance(s, datetime):
        return s
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except:
        return default
    return None

def sha256(s):
    """ Return the SHA256 HEX digest related to the specified string.
    """
    m = hashlib.sha256()
    m.update(bytes(s,"utf-8"))
    return m.hexdigest()

def abs_or_percent(value, default, max_value):
    v = default
    try: 
        if value.endswith("%") or value.endswith("p") or value.endswith("P"):
            v = math.ceil(float(value[:-1])/100.0 * max_value)
        else:
            v = int(value)
    except:
        pass
    return v    

def str2duration_seconds(s, no_exception=False, default=None):
    try:
        return int(s)
    except:
        try:
            # Parse timedelta metadata
            meta = s.split(",")
            metas = {}
            for m in meta:
                k, v = m.split("=")
                metas[k] = float(v)
            return timedelta(**metas).total_seconds()
        except Exception as e:
            if no_exception:
                return default
            raise e


def decode_json(value):
    if value is None:
        return None
    if value.startswith("b'"):
        value = value[2:][:-1]
    try:
        uncompress = gzip.decompress(base64.b64decode(value))
        value      = str(uncompress, "utf-8")
    except:
        pass
    return json.loads(value)

def encode_json(value, compress=False):
    value_j            = json.dumps(value, sort_keys=True, default=str)
    if compress:
        compressed = gzip.compress(bytes(value_j, "utf-8"), compresslevel=9)
        value_j    = str(base64.b64encode(compressed), "utf-8")
    return value_j


def Session():
    s = requests.Session()
    s.mount('file://', FileAdapter())
    return s

def put_url(url, value):
    # s3:// protocol management
    if url.startswith("s3://"):
        m = re.search("^s3://([-.\w]+)/(.*)", url)
        if len(m.groups()) != 2:
            return None
        bucket, key = [m.group(1), m.group(2)]
        key         = "/".join([p for p in key.split("/") if p != ""])
        client = boto3.client("s3")
        try:
            response = client.put_object(Bucket=bucket, Key=key, Body=bytes(value,"utf-8"))
            return True
        except Exception as e:
            log.warning(f"Failed to put data to S3 url '{url}' : {e}")
            return False
    log.warning(f"Unknown protocol '{url}' for put_url()")
    return False

def get_url(url, throw_exception_on_warning=False):
    def _warning(msg):
        if throw_exception_on_warning: 
            raise Exception(msg)
        else: 
            log.warning(msg)

    if url is None or url == "":
        return None

    # internal: protocol management
    internal_str = "internal:"
    if url.startswith(internal_str):
        filename = url[len(internal_str):]
        paths = [os.getcwd(), "/opt" ]
        if "LAMBDA_TASK_ROOT" in os.environ: 
            paths.insert(0, os.environ["LAMBDA_TASK_ROOT"])
        if "CLONESQUAD_DIR" in os.environ: 
            paths.append(os.environ["CLONESQUAD_DIR"])
            paths.append("%s/src/resources/" % os.environ["CLONESQUAD_DIR"])
        for path in paths:
            for sub_path in [".", "custo", "resources" ]:
                try:
                    f = open("%s/%s/%s" % (path, sub_path, filename), "rb")
                except:
                    continue
                return f.read()
        _warning("Fail to read internal url '%s'!" % url)
        return None


    # s3:// protocol management
    if url.startswith("s3://"):
        m = re.search("^s3://([-.\w]+)/(.*)", url)
        if len(m.groups()) != 2:
            return None
        bucket, key = [m.group(1), m.group(2)]
        key         = "/".join([p for p in key.split("/") if p != ""])
        client = boto3.client("s3")
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except Exception as e:
            _warning("Failed to fetch S3 url '%s' : %s" % (url, e))
            return None

    # <other>:// protocols management
    s      = Session()
    s.auth = IAMAuth()
    try:
        response = s.get(url)
    except Exception as e:
        _warning("Failed to fetch url '%s' : %s" % (url, e))
        return None
    if response is not None:
        return response.content
    return None

def put_s3_object(s3path, content):
    """ s3path: Format must be s3://<bucketname>/<key>
    """
    m = re.search("^s3://([-.\w]+)/(.*)", s3path)
    if len(m.groups()) != 2:
        return False
    bucket, key = [m.group(1), m.group(2)]
    key = "/".join([p for p in key.split("/") if p != ""]) # Remove extra '/'
    client = boto3.client("s3")
    try:
        response = client.put_object(
           Bucket=bucket,
           Key=key,
           Body=bytes(content, "utf-8"))
        return true
    except:
        return False

def parse_line_as_list_of_dict(string, with_leading_string=True, leading_keyname="_", default=None):
    if string is None:
        return default
    def _remove_escapes(s):
        return s.replace("\\;", ";").replace("\\,", ",").replace("\\=", "=")
    try:
        l = []
        for d in re.split("(?<!\\\\);", string):
            if d == "": continue

            dct       = defaultdict(str)
            el        = re.split("(?<!\\\\),", d)
            idx_start = 0
            if with_leading_string:
                key = el[0]
                if key == "": continue
                dct[leading_keyname] = _remove_escapes(key) #.replace("\\,", ",")
                idx_start = 1
            for item in el[idx_start:]:
                i_el = re.split("(?<!\\\\)=", item, maxsplit=1)
                dct[i_el[0]] = _remove_escapes(i_el[1]) if len(i_el) > 1 else True
            l.append(dct)
        return l
    except:
        return default

def dynamodb_table_scan(client, table_name, max_size=32*1024*1024):
    xray_recorder.begin_subsegment("misc.dynamodb_table_scan")
    items      = []
    items_size = []

    size     = 0 
    response = None
    paginator = client.get_paginator('scan')
    response_iterator = paginator.paginate(TableName=table_name, ConsistentRead=True)
    for response in response_iterator:
        if "Items" not in response: raise Exception("Failed to scan table '%s'!" % self.table_name)

        # Flatten the structure to make it more useable 
        for i in response["Items"]:
            item = {}
            for k in i:
                item[k] = i[k][list(i[k].keys())[0]]
            if "Key" in item and "Value" in item:
                items_size.append({"Key": item["Key"], "Size": len(item["Value"])})
            # Do not manage expired records
            if "ExpirationTime" in item:
                expiration_time = int(item["ExpirationTime"])
                if seconds_from_epoch_utc() > expiration_time:
                    continue
            if max_size != -1:
                item_size = 0
                for k in item: 
                    item_size += len(item[k])
                if size + item_size > max_size:
                    break # Truncate too big DynamoDB table
                else:
                    size += item_size
            items.append(item)
    log.log(log.NOTICE, f"DynamoDB: Table scan of '{table_name}' returned %d items (bytes={size})." % len(items))
    if log.getEffectiveLevel() == log.DEBUG:
        log.debug(f"Biggest items for table {table_name}:")
        sorted_items = sorted(items_size, key=lambda item: item["Size"], reverse=True)
        for i in sorted_items[:10]:
            log.debug(f"   Item: {i}")
    xray_recorder.end_subsegment()
    return items

@xray_recorder.capture()
def load_prerequisites(ctx, object_list):
    for o in object_list:
        xray_recorder.begin_subsegment("prereq:%s" % o)
        log.debug(f"Loading prerequisite '{o}'...")
        ctx[o].get_prerequisites()
        xray_recorder.end_subsegment()
    log.debug(f"End prerequisite loading...")

def initialize_clients(clients, ctx):
    config = Config(
       retries = {
       'max_attempts': 5,
       'mode': 'standard'
       })
    for c in clients:
        k = "%s.client" % c
        if k not in ctx:
            log.debug("Initialize client '%s'." % c)
            ctx[k] = boto3.client(c, config=config)

def discovery(ctx, via_discovery_lambda=False):
    """ Returns a discovery JSON dict of essential environment variables
    """
    if via_discovery_lambda:
        initialize_clients(["lambda"], ctx)
        client     = ctx["lambda.client"]
        group_name = ctx["GroupName"]
        region     = ctx["AWS_DEFAULT_REGION"]
        account_id = ctx["ACCOUNT_ID"]
        log.info("Calling Discovery Lambda 'CloneSquad-Discovery-{group_name}'...")
        response = client.invoke(
            FunctionName=f"arn:aws:lambda:{region}:{account_id}:function:CloneSquad-Discovery-{group_name}",
            InvocationType='RequestResponse',
            LogType='None',
            Payload=bytes('', "utf-8")
        )
        discovery = json.loads(str(response["Payload"].read(), "utf-8"))
    else:
        context = ctx.copy()
        for k in ctx.keys():
            if (k.startswith("AWS_") or k.startswith("_AWS_") or k.startswith("LAMBDA") or 
                    k.endswith("_SNSTopicArn") or
                    k in ["_HANDLER", "LD_LIBRARY_PATH", "LANG", "PATH", "TZ", "PYTHONPATH", "cwd", "FunctionName", "MainFunctionArn"] or 
                    not isinstance(context[k], str)):
                del context[k]
        if "ACCOUNT_ID" in context:
            context["AccountId"] = context["ACCOUNT_ID"]
            del context["ACCOUNT_ID"]
        if "CLONESQUAD_LOGLEVELS" in context:
            context["LogLevels"] = context["CLONESQUAD_LOGLEVELS"]
            del context["CLONESQUAD_LOGLEVELS"]

        if context["TimeZone"] == "":
            context["TimeZone"] = "UTC"

        # Get additional information (if we have enough rights to do so)
        try:
            response = ctx["organizations.client"].describe_account(AccountId=context["AccountId"])
            account = response["Account"]
            for k in [k for k in account.keys() if k not in ["Id"]]:
                context[f"Account{k}"] = account[k]
        except Exception as e:
            log.log(log.NOTICE, "Got exception while retrieving AWS Account details (IAM permission issue?) [It is a "
                f"minor event and will be safely ignored]: {e}")

        user_metadata = context["UserSuppliedJSONMetadata"]
        try:
            metadata = json.loads(user_metadata) if user_metadata != "" else {}
            if isinstance(metadata, dict):
                for k in metadata:
                    context[f"X-{k}"] = metadata[k]
            else:
                log.warning(f"'UserSuppliedJSONMetadata' CloudFormation template parameter must be a JSON dict! Ignoring supplied data...")
        except Exception as e:
            log.warning(f"Can not parse 'UserSuppliedJSONMetadata' CloudFormation template parameter as valid JSON document: {e}")
        discovery = json.loads(json.dumps(context, default=str))

    # Convert back some fields to date objects
    discovery["InstallTime"]                = str2utc(str(discovery["InstallTime"]))
    if "AccountJoinedTimestamp" in discovery:
        discovery["AccountJoinedTimestamp"] = str2utc(str(discovery["AccountJoinedTimestamp"]))

    return discovery

