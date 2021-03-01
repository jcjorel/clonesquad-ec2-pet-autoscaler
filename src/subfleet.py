
import boto3
import pdb
from datetime import datetime
from datetime import timedelta

import misc
import config as Cfg
import debug as Dbg

def get_subfleet_key(key, subfleet_name, cls=str, none_on_failure=False):
    all_val = Cfg.get(f"subfleet.__all__.{key}", cls=cls, none_on_failure=True)
    if all_val is not None:
        return all_val
    return Cfg.get(f"subfleet.{subfleet_name}.{key}", cls=cls, none_on_failure=none_on_failure)

def get_subfleet_key_abs_or_percent(key, subfleet_name, default, max_value):
    all_val = Cfg.get(f"subfleet.__all__.{key}", none_on_failure=True)
    if all_val is not None:
        return misc.abs_or_percent(all_val, default, max_value)
    value = Cfg.get(f"subfleet.{subfleet_name}.{key}")
    return misc.abs_or_percent(value, default, max_value)
