import os
import json
import yaml
from datetime import timedelta
import misc
import kvtable
import pdb
import debug as Dbg

import cslog
log = cslog.logger(__name__)

ctx   = None
_init = None

from aws_xray_sdk.core import xray_recorder

@xray_recorder.capture(name="config.init")
def init(context, with_kvtable=True, with_predefined_configuration=True):
    global _init
    _init                = {}
    _init["context"]     = context
    _init["all_configs"] = [{
        "source": "Built-in defaults",
        "config": {},
        "metas" : {}
        }]
    _init["dynamic_config"] = []
    _init["active_parameter_set"]    = None
    _init["with_kvtable"]            = False
    register({
             "config.dump_configuration,Stable" : {
                 "DefaultValue": "0",
                 "Format"      : "Bool",
                 "Description" : """Display all relevant configuration parameters in CloudWatch logs.

    Used for debugging purpose.
                 """
             },
             "config.loaded_files,Stable" : {
                 "DefaultValue" : "",
                 "Format"       : "StringList",
                 "Description"  : """A semi-column separated list of URL to load as configuration.

Upon startup, CloneSquad will load the listed files in sequence and stack them allowing override between layers.

Internally, 2 files are systematically loaded: 'internal:predefined.config.yaml' and 'internal:custom.config.yaml'. Users that intend to embed
customization directly inside the Lambda delivery should override file 'internal:custom.config.yaml' with their own configuration. See 
[Customizing the Lambda package](#customizing-the-lambda-package).

This key is evaluated again after each URL parsing meaning that a layer can redefine the 'config.loaded_files' to load further
YAML files.
                 """
             },
             "config.max_file_hierarchy_depth" : 10,
             "config.active_parameter_set,Stable": {
                 "DefaultValue": "",
                 "Format"      : "String",
                 "Description" : """Defines the parameter set to activate.

See [Parameter sets](#parameter-sets) documentation.
                 """
             },
             "config.default_ttl": 0,
             "config.cache.max_age": 60
    })
    if with_kvtable:
        _init["configuration_table"] = kvtable.KVTable.create(context, context["ConfigurationTable"],
                cache_max_age=get_duration_secs("config.cache.max_age"))
        _init["configuration_table"].reread_table()
        _init["with_kvtable"]        = True


    # Load extra configuration from specified URLs
    xray_recorder.begin_subsegment("config.init:load_files")
    files_to_load = ["internal:predefined.config.yaml", "internal:custom.config.yaml"]
    files_to_load.extend(get_list("config.loaded_files", default=[]) if with_predefined_configuration else [])
    if "ConfigurationURLs" in context:
        files_to_load.extend(context["ConfigurationURLs"].split(";"))
    if misc.is_sam_local():
        # For debugging purpose. Ability to override config when in SAM local
        resource_file = "internal:sam.local.config.yaml" 
        log.info("Reading local resource file %s..." % resource_file)
        files_to_load.append(resource_file)


    loaded_files = []
    i = 0
    while i < len(files_to_load):
        f = files_to_load[i]
        i += 1
        if f == "": 
            continue

        fd = None
        c  = None
        try:
            fd = misc.get_url(f, throw_exception_on_warning=True)
            c  = yaml.safe_load(fd)
            if c is None: c = [] # Empty YAML file
            loaded_files.append({
                    "source": f,
                    "config": c
                })
            if "config.loaded_files" in c and c["config.loaded_files"] != "":
                files_to_load.extend(c["config.loaded_files"].split(";"))
            if i > get_int("config.max_file_hierarchy_depth"):
                log.warning("Too much config file loads (%s)!! Stopping here!" % loaded_files) 
                break
        except Exception as e:
            if fd  is None: 
                log.warning("Failed to load config file '%s'! %s (Notice: It will be safely ignored!)" % (f, e))
            elif c is None: 
                log.warning("Failed to parse config file '%s'! %s (Notice: It will be safely ignored!)" % (f, e))
            else: 
                log.exception("Failed to process config file '%s'! (Notice: It will be safely ignored!)" % f)
    _init["loaded_files"] = loaded_files
    xray_recorder.end_subsegment()


    register({
        "config.ignored_warning_keys,Stable" : {
            "DefaultValue": "",
            "Format"      : "StringList",
            "Description" : """A list of config keys that are generating a warning on usage, to disable them.

Typical usage is to avoid the 'WARNING' Cloudwatch Alarm to trigger when using a non-Stable configuration key.

    Ex: ec2.schedule.key1;ec2.schedule.key2

    Remember that using non-stable configuration keys, is creating risk as semantic and/or existence could change 
    from CloneSquad version to version!
            """
            }
        })
    _init["ignored_warning_keys"] = get_list_of_dict("config.ignored_warning_keys")


def _parameterset_sanity_check():
    # Warn user if parameter set is not found
    active_parameter_set = _init["active_parameter_set"]
    if active_parameter_set is not None and active_parameter_set != "":
        found = False
        for cfg in _get_config_layers(reverse=True):
            c = cfg["config"]
            if _init["active_parameter_set"] in c:
                found = True
                break
        if not found:
            log.warning("Active parameter set is '%s' but no parameter set with this name exists!" % _init["active_parameter_set"])
    
def register(config, ignore_double_definition=False, layer="Built-in defaults", create_layer_when_needed=False):
    if _init is None:
        return
    layer_struct = next(filter(lambda l: l["source"] == layer, _init["all_configs"]), None)
    if layer_struct is None:
        if not create_layer_when_needed:
            raise Exception(f"Unknown config '{layer}'!")
        layer_struct     = {"source": layer, "config": {}, "metas": {}}
        _init["dynamic_config"].append(layer_struct)
    layer_config = layer_struct["config"]
    layer_metas  = layer_struct["metas"]
    for c in config:
        p = misc.parse_line_as_list_of_dict(c)
        key = p[0]["_"]
        if not ignore_double_definition and key in layer_config:
            raise Exception("Double definition of key '%s'!" % key)
        layer_config[key] = config[c]
        layer_metas[key]  = dict(p[0])

    # Build the config layer stack
    layers = []
    # Add built-in config
    layers.extend(_init["all_configs"])
    # Add file loaded config
    if "loaded_files" in _init:
        layers.extend(_init["loaded_files"])
    if _init["with_kvtable"]:
        # Add DynamoDB based configuration
        layers.extend([{
            "source": "DynamoDB configuration table '%s'" % _init["context"]["ConfigurationTable"],
            "config": _init["configuration_table"].get_dict()}])
    layers.extend(_init["dynamic_config"])
    _init["config_layers"] = layers

    # Update config.active_parameter_set
    builtin_config = _init["all_configs"][0]["config"]
    for cfg in _get_config_layers(reverse=True):
        c = cfg["config"]
        if "config.active_parameter_set" in c:
            if c == builtin_config and isinstance(c, dict):
                _init["active_parameter_set"] = c["config.active_parameter_set"]["DefaultValue"]
            else:
                _init["active_parameter_set"] = c["config.active_parameter_set"]
            break
    _parameterset_sanity_check()
    # Create a lookup efficient key cache
    compile_keys()


def _get_config_layers(reverse=False):
    if not reverse:
        return _init["config_layers"]
    l = _init["config_layers"].copy()
    l.reverse()
    return l

def _k(key):
    return key.replace("override:", "")

def is_stable_key(key):
    all_configs          = _init["all_configs"]
    metas                = all_configs[0]["metas"]
    return _k(key) in metas and "Stable" in metas[_k(key)] and metas[_k(key)]["Stable"]

def keys(prefix=None, only_stable_keys=False):
    all_configs          = _init["all_configs"]
    active_parameter_set = _init["active_parameter_set"]
    k                    = []
    config_layers        = _get_config_layers()
    for config_layer in _get_config_layers():
        c = config_layer["config"]
        for key in c:
            if key.startswith("#"): continue # Ignore commented keys
            if key.startswith("["): continue # Ignore parameterset keys
            if only_stable_keys and not is_stable_key(key):
                continue
            if prefix is not None and not key.startswith(prefix): continue
            if isinstance(c[key], list):
                continue # Ignore list() as it is erroneous
            if c != config_layers[0]["config"] and isinstance(c[key], dict):
                continue # We do not consider parameter set. On the Builtin layer, we accept dict that contains metas
            if key not in k:
                k.append(key)
    return k

def dumps(only_stable_keys=True):
    c = {}
    for k in keys(only_stable_keys=only_stable_keys):
        c[k] = get_extended(k).copy()
        del c[k]["Success"]
    return c

def dump():
    builtin_layer = _init["all_configs"][0]
    r             = dumps(only_stable_keys=False)
    keys          = {}
    for k in r:
        key_info = r[k]
        if "Stable" not in key_info:
            continue
        if key_info["Stable"]:
            keys[k] = key_info
            continue

        if "WARNING" in key_info["Status"]:
            pattern_match = False
            for pattern in _init["ignored_warning_keys"]: 
                if re.match(pattern, k):
                    pattern_match = True
            if not pattern_match:
                log.warning(key_info["Status"])

        if k in builtin_layer and key_info["ConfigurationOrigin"] != builtin_layer["source"]:
            log.warning("Non STABLE key '%s' defined in '%s'! /!\ WARNING /!\ Its semantic and/or existence MAY change in future CloneSquad release!!"
                % (k, key_info["ConfigurationOrigin"]))
            keys[k] = key_info

    if get_int("config.dump_configuration"):
        log.info(Dbg.pprint(keys))
        log.info("Loaded files: %s " % [ x["source"] for x in _init["loaded_files"]])

def is_builtin_key_exist(key):
    builtin_layer = _init["all_configs"][0]["config"]
    return key in builtin_layer

def compile_keys():
    """ Build a dictionary to quickly lookup keys.

    Note: This function searches 'override:{key}' before '{key}' names.
    """
    active_parameter_set   = _init["active_parameter_set"]
    builtin_layer          = _init["all_configs"][0]["config"]
    _init["compiled_keys"] = {}


    for key in keys(only_stable_keys=False):
        r = get_extended(key) # Retrieve the error structure.
        stable_key  = is_stable_key(key)
        r["Stable"] = stable_key

        key_def = None
        if key in builtin_layer and isinstance(builtin_layer[key], dict):
            key_def = builtin_layer[key]

        def _test_key(c, key):
            if key not in c or isinstance(c[key], list):
                return r
            if c != builtin_layer and isinstance(c[key], dict):
                return r
            pset_txt = " (ParameterSet='%s')" % parameter_set if parameter_set != "None" else ""
            res = {
                    "Success": True,
                    "ConfigurationOrigin" : config["source"],
                    "Status": "Key found in '%s'%s" % (config["source"], pset_txt),
                    "Stable": stable_key,
                    "Override": key.startswith("override:")
                }
            res["Value"] = c[key]
            if key_def is not None:
                for k in key_def:
                    res[k] = key_def[k]
                if c == builtin_layer:
                    res["Value"] = key_def["DefaultValue"]
            r.update(res)
            if _k(key) not in builtin_layer:
                r["Status"] = "[WARNING] Key '%s' doesn't exist as built-in default (Misconfiguration??) but %s!" % (key, r["Status"])
            return r

        # Perform 2 iterations: once to detect if there is an override and finally normal key lookup
        for key_pattern in [f"override:{key}", key]:
            for config in _get_config_layers(reverse=True):
                c = config["config"]

                parameter_set = "None"
                if not key_pattern.startswith("override:") and active_parameter_set in c:
                    if key in c[active_parameter_set]:
                        parameter_set = active_parameter_set
                        r = _test_key(c[active_parameter_set], key_pattern)
                        if r["Success"]: 
                            break

                r = _test_key(c, key_pattern)
                if r["Success"]: 
                    break
            if r["Success"]: 
                break
        _init["compiled_keys"][key] = r

def set(key, value, ttl=None):
    if _k(key) == "config.active_parameter_set":
        _init["active_parameter_set"] = value if value != "" else None
        _parameterset_sanity_check()
    if ttl is None: ttl = get_duration_secs("config.default_ttl")
    _init["configuration_table"].set_kv(key, value, TTL=ttl)

def get_direct_from_kv(key, default=None):
    t = kvtable.KVTable.create(ctx, ctx["ConfigurationTable"], cache_max_age=Cfg.get_duration_secs("config.cache.max_age"))
    v = t.get_kv(key, direct=True)
    return v if not None else default

def import_dict(c):
    t = _init["configuration_table"]
    t.set_dict(c)

def get_dict():
    t = _init["configuration_table"]
    return t.get_dict()

def get_extended(key, fmt=None):
    if key in _init["compiled_keys"]:
        r = _init["compiled_keys"][key]
    else:
        r = {
            "Key": key,
            "Value" : None,
            "Success" : False,
            "ConfigurationOrigin": "None",
            "Status": "[WARNING] Unknown configuration key '%s'" % key,
            "Stable": False,
            "Override": False
        }
    if fmt:
        r["Value"] = r["Value"].format(**fmt)
    return r

def get(key, cls=str, none_on_failure=False, fmt=None):
    r = get_extended(key, fmt=fmt)
    if not r["Success"]:
        if none_on_failure:
            return None
        else:
            raise Exception(r["Status"])
    try:
        if cls == str:
            return str(r["Value"]) if r["Value"] is not None else None
        if cls == int:
            return int(r["Value"])
        if cls == float:
            return float(r["Value"])
    except Exception as e:
        if none_on_failure:
            return None
        raise Exception(f"Failed to convert key '{key}' with value '%s' : {e}" % r["Value"])

def get_int(key, fmt=None):
    return get(key, cls=int, fmt=fmt)

def get_float(key, fmt=None):
    return get(key, cls=float, fmt=fmt)

def get_list(key, separator=";", default=None, fmt=None):
    v = get(key, fmt=fmt)
    if v is None or v == "": return default
    return [i for i in v.split(separator) if i != ""]

def get_duration_secs(key, fmt=None):
    try:
        return misc.str2duration_seconds(get(key, fmt=fmt))
    except Exception as e:
        raise Exception("[ERROR] Failed to parse config key '%s' as a duration! : %s" % (key, e))

def get_list_of_dict(key, fmt=None):
    v = get(key, fmt=fmt)
    if v is None: return []
    return misc.parse_line_as_list_of_dict(v)

def get_date(key, default=None, fmt=None):
    v = get(key, fmt=fmt)
    if v is None: return default
    return misc.str2utc(v, default=default)

def get_abs_or_percent(value_name, default, max_value, fmt=None):
    value = get(value_name, fmt=fmt)
    return misc.abs_or_percent(value, default, max_value)


