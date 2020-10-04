import os
import sys
import re
import hashlib
import json
import math

import gzip
# Hack: Force gzip to have a deterministic output (See https://stackoverflow.com/questions/264224/setting-the-gzip-timestamp-from-python/264303#264303)
class GzipFakeTime:
   def time(self):
      return 1.1
gzip.time = GzipFakeTime() 

import base64
import boto3
from datetime import datetime
from datetime import timezone
from datetime import timedelta
import requests
from requests_file import FileAdapter
from collections import defaultdict
import pdb
import debug as Dbg

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()


def is_sam_local():
    return "AWS_SAM_LOCAL" in os.environ and os.environ["AWS_SAM_LOCAL"] == "true"

import cslog
log = cslog.logger(__name__)

def is_direct_launch():
    return len(sys.argv) > 1

def utc_now():
    return datetime.now(tz=timezone.utc) # datetime.utcnow()

def epoch():
    return datetime.utcfromtimestamp(0).replace(tzinfo=timezone.utc)

def seconds_from_epoch_utc(now=None):
    if now is None: now = utc_now()
    return int((now - epoch()).total_seconds())

def seconds2utc(seconds):
    return datetime.utcfromtimestamp(int(seconds)).replace(tzinfo=timezone.utc)

def str2utc(s, default=None):
    try:
        return datetime.fromisoformat(s)
    except:
        return default
    return None

def sha256(s):
    m = hashlib.sha256()
    m.update(bytes(s,"utf-8"))
    return m.hexdigest()

def abs_or_percent(value, default, max_value):
    v = default
    try: 
        if value.endswith("%"):
            v = math.ceil(float(value[:-1])/100.0 * max_value)
        else:
            v = int(value)
    except:
        pass
    return v    

def str2duration_seconds(s):
    try:
        return int(s)
    except:
        # Parse timedelta metadata
        meta = s.split(",")
        metas = {}
        for m in meta:
            k, v = m.split("=")
            metas[k] = float(v)
        return timedelta(**metas).total_seconds()

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

url_cache = {}
def get_url(url):
    global url_cache
    if url is None or url == "":
        return None
    if url in url_cache:
        return url_cache[url]

    # internal: protocol management
    internal_str = "internal:"
    if url.startswith(internal_str):
        filename = url[len(internal_str):]
        paths = [os.getcwd(), "/opt" ]
        if "LAMBDA_TASK_ROOT" in os.environ: 
            paths.insert(0, os.environ["LAMBDA_TASK_ROOT"])
        if "CLONESQUAD_DIR" in os.environ: 
            paths.append(os.environ["CLONESQUAD_DIR"])
        for path in paths:
            for sub_path in [".", "custo", "resources" ]:
                try:
                    f = open("%s/%s/%s" % (path, sub_path, filename), "rb")
                except:
                    continue
                url_cache[url] = f.read()
                return url_cache[url]
        log.warning("Fail to read internal url '%s'!" % url)
        return None


    # s3:// protocol management
    if url.startswith("s3://"):
        m = re.search("^s3://([-.\w]+)/(.*)", url)
        if len(m.groups()) != 2:
            return None
        bucket, key = [m.group(1), m.group(2)]
        client = boto3.client("s3")
        try:
            response = client.get_object(
               Bucket=bucket,
               Key=key)
            url_cache[url] = response["Body"].read()
            return url_cache[url]
        except Exception as e:
            log.warning("Failed to fetch S3 url '%s' : %s" % (url, e))
            return None

    # <other>:// protocols management
    s = Session()
    try:
        response = s.get(url)
    except Exception as e:
        log.warning("Failed to fetch url '%s' : %s" % (url, e))
        return None
    if response is not None:
        url_cache[url] = response.content
        return url_cache[url]
    return None

def parse_line_as_list_of_dict(string, leading_keyname="_", default=None):
    if string is None:
        return default
    def _remove_escapes(s):
        return s.replace("\\;", ";").replace("\\,", ",").replace("\\=", "=")
    l = []
    for d in re.split("(?<!\\\\);", string):
        if d == "": continue

        el = re.split("(?<!\\\\),", d)
        key = el[0]
        if key == "": continue
        dct      = defaultdict(str)
        dct[leading_keyname] = _remove_escapes(key) #.replace("\\,", ",")
        for item in el[1:]:
            i_el = re.split("(?<!\\\\)=", item, maxsplit=1)
            dct[i_el[0]] = _remove_escapes(i_el[1]) if len(i_el) > 1 else True
        l.append(dct)
    return l

def dynamodb_table_scan(client, table_name, max_size=32*1024*1024):
    xray_recorder.begin_subsegment("misc.dynamodb_table_scan")
    items = []

    size     = 0 
    response = None
    while response is None or "LastEvaluatedKey" in response:
        query = {
                "TableName": table_name,
                "ConsistentRead": True
           }
        if response is not None and "LastEvaluatedKey" in response: query["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = client.scan(**query)

        if "Items" not in response: raise Exception("Failed to scan table '%s'!" % self.table_name)

        # Flatten the structure to make it more useable 
        for i in response["Items"]:
            item = {}
            for k in i:
                item[k] = i[k][list(i[k].keys())[0]]
            # Do not manage expired records
            if "ExpirationTime" in item:
                expiration_time = int(item["ExpirationTime"])
                if seconds_from_epoch_utc() > expiration_time:
                    continue
            if max_size != -1:
                item_size = 0
                for k in item: item_size += len(item[k])
                if size + item_size > max_size:
                    break # Truncate too big DynamoDB table
                else:
                    size += item_size
            items.append(item)
    log.debug("Table scan of '%s' returned %d items." % (table_name, len(items)))
    xray_recorder.end_subsegment()
    return items
