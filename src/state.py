import os
import pdb
import json
import debug
import debug as Dbg
import config
import kvtable
import misc

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class StateManager:
    def __init__(self, context):
        self.context = context
        self.table = None
        self.table_aggregates = []

    def get_prerequisites(self):
        ctx        = self.context
        self.table = kvtable.KVTable.create(self.context, self.context["StateTable"], use_cache=ctx["FunctionName"] == "Main")
        for a in self.table_aggregates:
            self.table.register_aggregates(a)
        self.table.reread_table()

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
