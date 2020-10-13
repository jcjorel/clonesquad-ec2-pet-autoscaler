import json
import io
import yaml
import re
import zipfile
import pdb
import base64
import gzip

import config as Cfg
import misc

import cslog
log = cslog.logger(__name__)

def pprint(json_obj):
    return json.dumps(json_obj, indent=4, sort_keys=True, default=str)


def debug_report_generator(ctx):
    report = {
        "GenerationDate": misc.utc_now()
      }

    # Collect all DynamoDB table content
    report["DynamoDBTables"] = {}
    for i in ctx:
        if i.endswith("Table"):
            table_name = ctx[i]
            try:
                table_data = misc.dynamodb_table_scan(ctx["dynamodb.client"], table_name)
            except Exception as e:
                log.exception("Failed to retrieve DynamoDB table '%s' : %s" % (table_name, e))
                continue
            report["DynamoDBTables"][table_name] = table_data
    return report

def _account_id_detector(s, detected_strings):
    replacements = []
    for m in re.findall("arn:[a-z]+:[a-z]+:[-a-z0-9]+:([0-9]+):.+", json.dumps(s)):
        replacements.append({
            "Keyword": m,
            "Replacement": "XXXXXXXXXXXX"
            })
    return replacements

def _metadata_data_obfuscator(s, detected_strings):
    try:
        metadata = base64.b64decode(s)
        metadata = gzip.decompress(metadata)
        metadata = str(metadata, "utf-8")
        for s in detected_strings:
            metadata = metadata.replace(s["Keyword"], s["Replacement"])
    except: pass
    try:
        metadata = json.loads(metadata)
        if "EC2" in metadata:
            instances = metadata["EC2"]["AllInstanceDetails"]
            for i in instances:
                for tags in i["Tags"]:
                    if tags["Key"].startswith("CloneSquad"):
                        continue
                    tags["Value"] = "OBFUSCATED"
            return base64.b64encode(
                      gzip.compress(bytes(json.dumps(metadata), "utf-8"))
                   )
    except: pass
    return "<Exception>"
        

def _metadata_data_detector(s, detected_strings):
    replacements = []
    try:
        metadata = base64.b64decode(s)
        metadata = gzip.decompress(metadata)
        metadata = str(metadata, "utf-8")
        replacements.extend(_account_id_detector(metadata, detected_strings))
    except: pass
    try:
        metadata = json.loads(metadata)
        if "EC2" in metadata:
            instances = metadata["EC2"]["AllInstanceDetails"]
            for i in instances:
                replacements.extend([{
                    "Keyword": i["KeyName"],
                    "Replacement": "SSH_KEY_NAME_OBFUSCATED"
                    },
                    {
                    "Keyword": i["PrivateDnsName"],
                    "Replacement": "PRIVATE_DNS_NAME_OBFUSCATED"
                    },
                    {
                    "Keyword": i["PrivateIpAddress"],
                    "Replacement": "PRIVATE_IP_ADDRESS_OBFUSCATED"
                    },
                    {
                    "Keyword": i["ImageId"],
                    "Replacement": "IMAGEID_OBFUSCATED"
                    }
                    ])
                if "PublicDnsName" in i:
                    replacements.extend([{
                        "Keyword": i["PublicDnsName"],
                        "Replacement": "PUBLIC_DNS_NAME_OBFUSCATED"
                        }])
                if "PublicIpAddress" in i:
                    replacements.extend([{
                        "Keyword": i["PublicIpAddress"],
                        "Replacement": "PUBLIC_IP_ADDRESS_OBFUSCATED"
                        }])

                for eni in i["NetworkInterfaces"]:
                    for ip in eni["PrivateIpAddresses"]:
                        replacements.extend([{
                            "Keyword": ip["PrivateIpAddress"],
                            "Replacement": "PRIVATE_IP_ADDRESS_OBFUSCATED"
                            },
                            {
                            "Keyword": ip["PrivateDnsName"],
                            "Replacement": "PRIVATE_DNS_NAME_OBFUSCATED"
                            }
                            ])
    except Exception as e: 
        log.exception("[WARNING] Failed to detect sensitive data in metadata : %s " % e)
    return replacements

def _replace_strings(s, detected_sensitive_strings):
    for sensitive in detected_sensitive_strings:
        s = s.replace(sensitive["Keyword"], sensitive["Replacement"])
    return s

def debug_report_obfuscate(ctx, report):
    sensitive_columns = [
            { "ColumnName": "Metadata",
              "Detectors": [ _metadata_data_detector ],
              "Obfuscators": [ _metadata_data_obfuscator ]
            },
            { "ColumnName": "HandledException",
              "Detectors": [ _account_id_detector ],
              "Obfuscators": [ _replace_strings ]
            },
            { "ColumnName": "InputData",
              "Detectors": [ _account_id_detector ],
              "Obfuscators": [ _replace_strings ]
            },
            { "ColumnName": "OutputData",
              "Detectors": [ _account_id_detector ],
              "Obfuscators": [ _replace_strings ]
            },
            { "ColumnName": ".*_Event",
              "Detectors": [ _account_id_detector ],
              "Obfuscators": [ _replace_strings ]
            }
        ]

    detected_sensitive_strings = []
    # Detect and Obfuscate
    for step in ["Detectors", "Obfuscators"]:
        for table_name in report["DynamoDBTables"]:
            table = report["DynamoDBTables"][table_name]
            for row in table:
                for c in row:
                    for col in sensitive_columns:
                        if not re.match(col["ColumnName"], c):
                            continue
                        r = row[c]
                        for o in col[step]:
                            if step == "Detectors": 
                                for s in o(r, detected_sensitive_strings):
                                    all_strings = [ s["Keyword"] for s in detected_sensitive_strings ]
                                    keyword = s["Keyword"]
                                    if keyword != "" and keyword not in all_strings:
                                        detected_sensitive_strings.append(s)
                            else: 
                                r = o(r, detected_sensitive_strings)
                        row[c] = r
    return report

def debug_report_publish(ctx, report, url, reportname="report", now=None):
    client = ctx["s3.client"]

    if not url.startswith("s3://"):
        log.error("Url '%s' for debug report must start with s3://... !" % url)
        return False

    m = re.search("^s3://([-.\w]+)/(.*)", url)
    if len(m.groups()) != 2:
        log.error("Failed to parse S3 url '%s'! Debug report NOT publiched!" % url)
        return False
    bucket_name = m.group(1)
    # Ensure no extra '/' in the path
    key         = [ p for p in m.group(2).split("/") if p != "" ]
    key         = "/".join(key)


    with zipfile.ZipFile('/tmp/report.zip', mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zip_f:
        # Dump per table data
        dynamo_tables = report["DynamoDBTables"]
        for t in dynamo_tables:
            table     = report["DynamoDBTables"][t]
            directory = "DynameDBTable-%s" % (t) 
            for row in table:
                png_columns = [ c for c in row.keys() if c.endswith("_PNG") ]
                for png in png_columns:
                    filename  = row["EventDate"].replace(" ","_")
                    filename += "_"
                    filename += png.replace("_PNG", ".png")
                    with zip_f.open("%s/%s" % (directory, filename), "w") as z:
                        z.write(base64.b64decode(row[png]))

            with zip_f.open("%s/tablecontent.yaml" % directory, "w") as z:
                z.write(bytes(yaml.dump(table),"utf-8"))

        del report["DynamoDBTables"]
        with zip_f.open("report.yaml", "w") as z:
            z.write(bytes(yaml.dump(table),"utf-8"))
        report["DynamoDBTables"] = dynamo_tables

    n = ctx["now"] if now is None else now
    k = "%s/%s_%s.zip" % (key, str(n).replace(" ", "_"), reportname)
    client.upload_file("/tmp/report.zip", bucket_name, k)
    log.info("Uploaded 's3://%s/%s'." % (bucket_name, k))

    return True

recurse_protection = False
def publish_all_reports(ctx, url, reportname, now=None):
    global recurse_protection
    if recurse_protection: return
    recurse_protection = True

    try:
        log.info("Generating debug report in memory...")
        report = debug_report_generator(ctx)
        log.info("Publishing to S3 (clear text)...")
        debug_report_publish(ctx, report, url, reportname=reportname, now=now)
        log.info("Obfuscating...")
        if Cfg.get_int("notify.debug.obfuscate_s3_reports"):
            report = debug_report_obfuscate(ctx, report)
        log.info("Publishing to S3 (obfuscated)...")
        debug_report_publish(ctx, report, url, reportname="%s_OBFUSCATED" % reportname, now=now)
    except Exception as e:
        log.exception("[ERROR] Failed to send debug report to S3 (%s) : %s" % (url, e))
    log.info("Uploaded debug report to S3...")
    recurse_protection = False

def manage_publish_report(ctx, event, response):
    url = ctx["LoggingS3Path"]
    if url != "":
        # Send a comprehensive debug report to S3. WARNING: Takes time!
        n = ctx["Timestamp"] if "Timestamp" in ctx else None
        publish_all_reports(ctx, url, "notifymgr_report", now=n)
        response["statusCode"] = 200
        response["body"]       = "Debug reports exported to '%s'" % url
        response["headers"]["Content-Type"] = "text/plain"
    else:
        response["statusCode"] = 503
        response["body"]       = "No 'LoggingS3Path' configured. Can't export debug report"
        response["headers"]["Content-Type"] = "text/plain"
    return True



