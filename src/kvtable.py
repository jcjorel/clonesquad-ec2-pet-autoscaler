import os
import pdb
import re
import misc
import json
import yaml
from datetime import datetime
from datetime import timedelta
from collections import defaultdict
import debug as Dbg
import config as Cfg
import boto3

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

all_kv_objects = []

class KVTable():
    def __init__(self, context, table_name, aggregates=None):
        global all_kv_objects
        self.context     = context
        self.table_name  = table_name
        self.table_cache = None
        self.dict_struct = None
        self.aggregates  = aggregates if aggregates is not None else []
        self.table_schema= None
        self.is_kv_table = False
        self.table_last_read_date = None
        existing_object = next(filter(lambda o: o.table_name == table_name, all_kv_objects), None)
        if existing_object is not None:
            all_kv_objects.remove(existing_object)
        self.table_cache_dirty = False
        all_kv_objects.append(self)

    def _get_cache_for_tablename(table_name):
        return next(filter(lambda o: o.table_name == table_name, all_kv_objects), None)

    def create(context, table_name, aggregates=None, cache_max_age=0):
        existing_object = KVTable._get_cache_for_tablename(table_name)
        if cache_max_age == 0 or existing_object is None:
            return KVTable(context, table_name, aggregates=aggregates)

        if (existing_object.table_last_read_date is not None and 
                (context["now"] - existing_object.table_last_read_date).total_seconds() > cache_max_age):
                return KVTable(context, table_name, aggregates=aggregates)

        flush = KVTable.get_kv_direct("cache.last_write_index", table_name)
        if flush != existing_object.get_kv("cache.last_write_index"):
            return KVTable(context, table_name, aggregates=aggregates)
        log.debug(f"Lambda cache reuse for table {table_name}...")
        return existing_object

    def reread_table(self, force_reread=False):
        if not force_reread and self.table_cache is not None:
            return

        now    = self.context["now"]
        misc.initialize_clients(["dynamodb"], self.context)
        client = self.context["dynamodb.client"]

        self.table_last_read_date = now

        # Get table schema
        response = client.describe_table(
                TableName=self.table_name
            )
        self.table_schema = response["Table"]
        schema            = self.table_schema["KeySchema"]
        self.is_kv_table  = len(schema) == 1 and schema[0]["AttributeName"] == "Key"
    

        # Read all the table into memory
        table_content = None
        try:
            table_content = misc.dynamodb_table_scan(client, self.table_name)
        except Exception as e:
            log.exception("Failed to scan '%s' table: %s" % (self.table_name, e))
            raise e

        table_cache =  []
        # Extract aggregates when encountering them
        for record in table_content:
            if "Key" not in record: 
                table_cache.append(record)
                continue
            key       = record["Key"]
            if "Value" not in record:
                log.warn("Key '%s' specified but missing 'Value' column in configuration record: %s" % (key, record))
                continue
            value     = record["Value"]

            aggregate = next(filter(lambda a: key == a["Prefix"], self.aggregates), None)
            if aggregate is not None:
                agg = []
                try:
                    agg = misc.decode_json(value)
                except Exception as e:
                    log.debug("Failed to decode JSON aggregate for key '%s' : %s / %s " % (key, value, e))
                    continue
                agg.append(record)
                self._safe_key_import(table_cache, aggregate, agg, exclude_aggregate_key=False)
            else:
                aggregate = next(filter(lambda a: self.is_aggregated_key(key), self.aggregates), None)
                if aggregate:
                    log.debug("Found a record '%s' that should belong to an aggregate. Ignoring it!" % key)
                    continue
                self._safe_key_import(table_cache, aggregate, [record])
        # Clean the table of outdated record (TTL based)
        self.table_cache = []
        for r in table_cache:
            if "ExpirationTime" not in r:
                self.table_cache.append(r)
                continue
            expiration_time = misc.seconds2utc(r["ExpirationTime"])
            if expiration_time is None or expiration_time > now:
                self.table_cache.append(r)
            else:
                if self.is_kv_table:
                    log.debug("Wiping outdated item '%s'..." % r["Key"])
                    client.delete_item(Key={
                        'Key': {'S': r["Key"]},
                       },
                       TableName=self.table_name
                       )


        # Build an easier to manipulate dict of all the data
        self._build_dict()

    def _safe_key_import(self, dest_list, aggregate, records, exclude_aggregate_key=True):
        now           = misc.seconds_from_epoch_utc(now=self.context["now"])
        existing_keys = [d["Key"] for d in dest_list]
        if aggregate is None:
            for r in records:
                key    = r["Key"]
                if key in existing_keys:
                    log.error("Duplicate KV key '%s'!" % key)
                    continue
                if "ExpirationTime" in r:
                    if int(r["ExpirationTime"]) < now:
                        continue
                existing_keys.append(key)
                dest_list.append(r)
            return
                

        excludes      = aggregate["Exclude"] if "Exclude" in aggregate else []
        prefix        = aggregate["Prefix"]
        for r in records:
            if "ExpirationTime" in r:
                if int(r["ExpirationTime"]) < now:
                    continue
            key    = r["Key"]
            if not key.startswith(prefix) or (exclude_aggregate_key and key == prefix) or (exclude_aggregate_key and key.endswith(".")):
                continue
            if key in existing_keys:
                log.error("Duplicate KV key '%s'!" % key)
                continue
            excluded = next(filter(lambda e: key.startswith(e) and key != e, excludes), None)
            if excluded is not None:
                continue
            existing_keys.append(key)
            dest_list.append(r)


    def register_aggregates(self, aggregate):
        self.aggregates.extend(aggregate)

    def is_aggregated_key(self, key):
        if not self.is_kv_table:
            return False
        for a in self.aggregates:
            if key.startswith(a["Prefix"]) and key != a["Prefix"] and not key.endswith("."):
                excludes = a["Exclude"] if "Exclude" in a else []
                r = next(filter(lambda e: key.startswith(e) and key != e, excludes), None)
                if r is not None:
                    continue
                return True
        return False

    def compare_kv_list(left, right):
        delta = 0
        if right is None: right = []
        lst = {
                "List": left,
                "Name": "Left",
                "Opposite": 1,
                "Keys": [i["Key"] for i in left],
                "DeepCompare": True
            }, {
                "List": right,
                "Name": "Right",
                "Opposite": 0,
                "Keys": [i["Key"] for i in right],
                "DeepCompare": False
            }
        for side in lst:
            for kv in side["List"]:
                key   = kv["Key"]
                if key not in lst[side["Opposite"]]["Keys"]:
                    log.debug("Key '%s' in '%s' doesn't exist in '%s'!" % (key, side["Name"], lst[side["Opposite"]]["Name"]))
                    delta += 1
                    continue
                if not side["DeepCompare"]:
                    continue
                value = kv["Value"]
                opposite_record = next(filter(lambda r: key == r["Key"], lst[side["Opposite"]]["List"])) 
                if value != opposite_record["Value"]:
                    log.debug("Key '%s' values differ! [%s <=> %s]" % (key, value, opposite_record["Value"]))
                    delta += 1
                    continue
                if bool("ExpirationTime" in kv) ^ bool("ExpirationTime" in opposite_record):
                    log.debug("Key '%s' has not same ExpirationTime presence!" % key)
                    delta += 1
                    continue
                expiration_time = kv["ExpirationTime"]
                if expiration_time != opposite_record["ExpirationTime"]:
                    delta += 1
                    log.debug("ExpirationTimes for Key '%s' have different values! [%s <=> %s]" %
                        (key, expiration_time, opposite_record["ExpirationTime"]))
        return delta


    def persist_aggregates():
        global all_kv_objects
        seconds_from_epoch = misc.seconds_from_epoch_utc()
        for t in all_kv_objects:
            if t.table_cache is None or not t.is_kv_table or len(t.aggregates) == 0:
                continue
            log.debug("Persisting aggregates for KV table '%s'..." % t.table_name)
            xray_recorder.begin_subsegment("persist_aggregates.%s" % t.table_name)
            for aggregate in t.aggregates:
                serialized = []
                ttl        = 0
                compress   = aggregate["Compress"] if "Compress" in aggregate else False
                prefix     = aggregate["Prefix"]
                t._safe_key_import(serialized, aggregate, t.table_cache)
                for i in serialized:
                    ttl = max(ttl, i["ExpirationTime"] - seconds_from_epoch) if "ExpirationTime" in i else ttl
                if len(serialized) == 0: 
                    ttl = aggregate["DefaultTTL"]
                if log.level == log.DEBUG:
                    log.log(log.NOTICE, "Delta between aggregate '%s' in DynamoDB and the new one:" % prefix)
                    #if misc.encode_json(serialized, compress=compress) == t.get_kv(prefix): pdb.set_trace()
                    if KVTable.compare_kv_list(serialized, misc.decode_json(t.get_kv(prefix))) == 0: pass #pdb.set_trace()
                    log.log(log.NOTICE, "Delta end.")
                t.set_kv(prefix, misc.encode_json(serialized, compress=compress), TTL=ttl)
            if t.table_cache_dirty:
                t.set_kv("cache.last_write_index", t.context["now"], TTL=0)
            xray_recorder.end_subsegment()

    def _build_dict(self):
        d = defaultdict(dict)
        for i in self.table_cache:
            if "Key" not in i:
                # Ignore this item without mandatory 'Key' column
                continue
            key = i["Key"]
            m = re.search("^\[([^\]]+)\](.*)", key)
            if m is not None and len(m.groups()) > 1:
                partition            = m.group(1)
                subkey               = m.group(2)
                d[partition][subkey] = i["Value"]
            else:
                d[key]               = i["Value"]
        self.dict_struct = dict(d)

    def get_items(self):
        return self.table_cache

    def get_dict(self):
        return self.dict_struct

    def set_dict(self, d, TTL=None):
        for k in d:
            v = d[k]
            if isinstance(v, dict):
                for i in v:
                    self.set_kv(i, v[i], partition=k, TTL=TTL)
            else:
                self.set_kv(k, v, TTL=TTL)

            

    def get_keys(self, prefix=None, partition=None):
        def _enum(dt):
            for k in dt:
                if prefix is not None and not k.startswith(prefix): continue 
                if not isinstance(dt[k], dict) and not isinstance(dt[k], list):
                    keys.append(k)

        keys = []
        d = self.get_dict()
        if partition in d and isinstance(d[partition], dict):
            _enum(d[partition])
        _enum(d)
        return keys

    def get_item(self, key, partition=None):
        now = self.context["now"]
        k   = key if partition is None else "[%s]%s" % (partition, key)
        for item in self.table_cache:
            if "Key" in item and item["Key"] == k:
                if "ExpirationTime" not in item or int(item["ExpirationTime"]) > misc.seconds_from_epoch_utc(now=now):
                    return item
                # Expired record. Garbage collect it now...
                self.table_cache.remove(item)
                self._build_dict()
                return None
        return None

    def get_kv_direct(key, table_name, default=None, context=None, TTL=None):
        if context is None:
            client = boto3.client("dynamodb")
        else:
            client = context["dynamodb.client"]
        query  = {
                 'Key': {
                     'S': key
                 }
        }

        response = client.get_item(
            TableName=table_name,
            ConsistentRead=True,
            ReturnConsumedCapacity='TOTAL',
            Key=query
            )
        log.log(log.NOTICE, f"DynamoDB({table_name}): Direct read of item '[{table_name}]{key}'")
        if "Item" not in response:
            return default
        item  = response["Item"]
        value = item["Value"][list(item["Value"].keys())[0]]
        if TTL != 0 and TTL is not None:
            # Refresh TTL
            set_kv_direct(key, value, table_name, partition=partition, TTL=TTL, context=context)
        return value

    def set_kv_direct(key, value, table_name, partition=None, TTL=None, context=None):
        if context is None:
            client = boto3.client("dynamodb")
            now    = None
        else:
            client = context["dynamodb.client"]
            now    = context["now"]
        log.debug("KVTable: dynamodb.put_item(TableName=%s, Key=%s, Value='%s'" % (table_name, key, value))
        existing_object = KVTable._get_cache_for_tablename(table_name)
        if existing_object is not None:
            existing_object.table_cache_dirty = True
        if value is None or str(value) == "":
            client.delete_item(
                Key = {"Key": {"S": key}},
                TableName=table_name
                )
        else:
            query = {
                 'Key': {
                     'S': key
                 },
                'Value': {
                    'S': str(value)
                }
            }
            if TTL != 0 and TTL is not None:
                expiration_time = misc.seconds_from_epoch_utc(now=now) + TTL
                query.update({
                'ExpirationTime' : {
                    'N': str(expiration_time)
                }})
            log.log(log.NOTICE, f"DynamoDB({table_name}): Writing key '{key}' (TTL={TTL}, size=%s)" % len(str(value)))

            response = client.put_item(
                TableName=table_name,
                ReturnConsumedCapacity='TOTAL',
                Item=query
                )

    def get_kv(self, key, partition=None, default=None, direct=False, TTL=None):
        if direct:
            return self.get_kv_direct(key, self.table_name, default=default, TTL=TTL)

        client = self.context["dynamodb.client"]
        item = self.get_item(key, partition=partition)
        if item is None:
            return default
        if TTL != 0 and TTL is not None:
            self.set_kv(key, item["Value"], partition=partition, TTL=TTL)
        return item["Value"]

    def set_kv(self, key, value, partition=None, TTL=None):
        now      = self.context["now"]
        now_secs = misc.seconds_from_epoch_utc(now=now)
        client   = self.context["dynamodb.client"]
        if TTL is None:
            ttl = 0
        else:
            ttl = int(TTL)

        k = key if partition is None else "[%s]%s" % (partition, key)

        # Optimize writes to KV table to reduce cost: Only write to the KV
        #   when value is different than in the cache or half-way of 
        #   expiration time
        if self.table_cache is not None:
            item = self.get_item(key, partition=partition)
            if item is not None and "ExpirationTime" in item and item["Value"] == str(value):
                if (now_secs + (ttl/2)) < int(item["ExpirationTime"]):
                    # Renew the ExpirationTime (Important for aggregated keys)
                    item["ExpirationTime"] = int(now_secs + ttl)
                    self.table_cache_dirty = True
                    log.debug(f"KVtable: Optimized write to '{k}' with value '{value}'")
                    return
                else:
                    log.debug(f"KVtable: Key {k} needs refresh (TTL passed mid-life)")
                    # Fall through...

        if not self.is_aggregated_key(k):
            KVTable.set_kv_direct(k, value, self.table_name, TTL=ttl, context=self.context)

        # Update cache
        if self.table_cache is None: # KV_Table not yet initialized
            return
        expiration_time = now_secs + ttl
        new_item = {
                "Key": k,
                "Value": str(value),
                "ExpirationTime": int(expiration_time)
            }

        if item is not None:
            if str(value) == "":
                self.table_cache.remove(item)
            else:
                item.update(new_item)
        else:
            self.table_cache.append(new_item)

        # Rebuild the dict representation
        self._build_dict()

    def export_to_s3(self, url, suffix, prefix="", athena_search_format=False):
        account_id = self.context["ACCOUNT_ID"]
        region     = self.context["AWS_DEFAULT_REGION"]
        group_name = self.context["GroupName"]
        now        = self.context["now"]
        path       = (f"{url}/accountid={account_id}/region={region}/groupname={group_name}/"
                         f"{prefix}{account_id}-{region}-{suffix}-cs-{group_name}")
        if not athena_search_format:
            misc.put_url(f"{path}.yaml", yaml.dump(self.get_dict()))
        else:
            dump = []
            for k in self.get_keys():
                item = {
                    "Key": k,
                    "Value": self.get_kv(k),
                    "MetadataRecordLastUpdatedAt": now
                }
                dump.append(json.dumps(item, default=str))
            misc.put_url(f"{path}.json", "\n".join(dump))
                


