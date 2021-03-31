import os
import pdb
import json
import debug
import debug as Dbg
import config
import kvtable
import misc
import re
import itertools
from datetime import datetime
from datetime import timedelta

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class StateManager:
    def __init__(self, context):
        self.context               = context
        self.table                 = None
        self.table_aggregates      = []
        self.clonesquad_resources = []

    def get_prerequisites(self):
        ctx        = self.context
        self.table = kvtable.KVTable.create(self.context, self.context["StateTable"], use_cache=ctx["FunctionName"] == "Main")
        for a in self.table_aggregates:
            self.table.register_aggregates(a)
        self.table.reread_table()

        # Retrieve all CloneSquad resources
        misc.initialize_clients(["resourcegroupstaggingapi"], self.context)
        tagging_client = self.context["resourcegroupstaggingapi.client"]
        paginator      = tagging_client.get_paginator('get_resources')
        tag_mappings   = itertools.chain.from_iterable(
            page['ResourceTagMappingList']
                for page in paginator.paginate(
                    TagFilters=[
                        {
                            'Key': 'clonesquad:group-name',
                            'Values': [ self.context["GroupName"] ]
                        }]
                    )
            )
        self.clonesquad_resources = list(tag_mappings)

    def get_resource_services(self):
        """ Return the list of services with clonesquuad:group-name tags
        """
        services = []
        for r in self.get_resources():
            arn = r["ResourceARN"]
            m   = re.search("^arn:[a-z]+:([a-z0-9]+):([-a-z0-9]+):([0-9]+):(.+)", arn)
            if m[1] not in services:
                services.append(m[1])
        return services

    def get_resources(self, service=None, resource_name=None):
        resources = []
        for t in self.clonesquad_resources:
            arn            = t["ResourceARN"]
            current_region = self.context["AWS_DEFAULT_REGION"]
            m              = re.search("^arn:[a-z]+:([a-z0-9]+):([-a-z0-9]+):([0-9]+):(.+)", arn)
            if m[2] != current_region:
                continue
            if service is not None and m[1] != service:
                continue
            if resource_name is not None and not re.match(resource_name, m[4]):
                continue
            resources.append(t)
        return resources

    def get_state_table(self):
        return self.table

    def register_aggregates(self, aggregates):
        self.table_aggregates.append(aggregates)

    def get_metastring_list(self, key, default=None, TTL=None):
        value = self.get_state(key, default=default, TTL=TTL)
        return misc.parse_line_as_list_of_dict(value, default=default)

    def get_metastring(self, key, default=None, TTL=None):
        value = get_metastring_list(key, default=default, TTL=TTL)
        if value is None or len(value) == 0:
            return default
        return value[0]

    def set_state(self, key, value, direct=False, TTL=0):
        if direct or self.table is None:
            kvtable.KVTable.set_kv_direct(key, value, self.context["StateTable"], TTL=TTL)
        else:
            self.table.set_kv(key, value, TTL=TTL)

    def get_state(self, key, default=None, direct=False, TTL=None):
        if direct or self.table is None:
            return kvtable.KVTable.get_kv_direct(key, self.context["StateTable"], default=default, TTL=TTL)
        else:
            return self.table.get_kv(key, default=default, TTL=TTL)

    def get_state_date(self, key, default=None, direct=False, TTL=None):
        d = self.get_state(key, default=default, direct=direct, TTL=TTL)
        if d is None or d == "": return default
        try:
            date = datetime.fromisoformat(d)
        except:
            return default
        return date

    def get_state_int(self, key, default=0, direct=False, TTL=None):
        try:
            return int(self.get_state(key, direct=direct, TTL=None))
        except:
            return default


    def get_state_json(self, key, default=None, direct=False, TTL=None):
        try:
            v = misc.decode_json(self.get_state(key, direct=direct, TTL=TTL))
            return v if v is not None else default
        except:
            return default

    def set_state_json(self, key, value, compress=True, TTL=0):
        self.set_state(key, misc.encode_json(value, compress=compress), TTL=TTL)
