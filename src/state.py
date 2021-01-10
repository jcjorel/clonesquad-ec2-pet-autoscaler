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

    def get_resources(self, service=None, resource_name=None):
        resources = []
        for t in self.clonesquad_resources:
            arn            = t["ResourceARN"]
            current_region = self.context["AWS_DEFAULT_REGION"]
            m = re.search("^arn:[a-z]+:([a-z0-9]+):([-a-z0-9]+):([0-9]+):(.+)", arn)
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

    def get_metastring_list(self, key, default=None):
        value = self.table.get_kv(key)
        return misc.parse_line_as_list_of_dict(value, default=default)

    def get_metastring(self, key, default=None):
        value = get_metastring_list(key)
        if value is None or len(value) == 0:
            return default
        return value[0]

    def set_state(self, key, value, TTL=0):
        self.table.set_kv(key, value, TTL=TTL)

    def get_state(self, key, direct=False):
        if direct:
            return kvtable.KVTable.get_kv_direct(key, self.context["StateTable"])
        else:
            return self.table.get_kv(key)

