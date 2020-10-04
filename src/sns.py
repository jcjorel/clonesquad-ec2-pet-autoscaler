import os
import json
from datetime import datetime
from datetime import timezone
import time
import boto3

import sqs
import misc
import config as Cfg
import debug as Dbg

import cslog
log = cslog.logger(__name__)

class SNSMgr:
    def __init__(self, context, ec2):
        self.context = context
        self.ec2     = ec2
        Cfg.register( {
           "snsmgr.record_expiration_delay" : "hours=1"
        })


    def handler(self, event, context):
        # Protect from bad data and keep only SNS messages
        if "Records" not in event:
            log.error("Not a valid SNS event")
            return

        sns_records = []
        for sns_msg in event["Records"]:
            if "EventSource" in sns_msg and sns_msg["EventSource"] == "aws:sns":
                try:
                    sns_msg["_decoded_message"] = json.loads(sns_msg["Sns"]["Message"])
                    sns_records.append(sns_msg)
                except Exception as e:
                    log.exception("Failed to decode message %s : %s" % (sns_msg, e))

        log.debug(Dbg.pprint(sns_records))

        need_main_update = False

        # For each SNS records, we keep track of important data in
        #    a DynamoDB table
        for sns_msg in sns_records:
            message          = sns_msg["_decoded_message"]
            timestamp        = datetime.fromisoformat(message["StateChangeTime"].replace("+0000","")).replace(tzinfo=timezone.utc) 
            alarm_name       = message["AlarmName"]
            new_state_reason = message["NewStateReason"]
            new_state_value  = message["NewStateValue"]
            namespace        = message["Trigger"]["Namespace"]
            metric_name      = message["Trigger"]["MetricName"]
            dimensions       = message["Trigger"]["Dimensions"]
            instance_id      = "None"
            try:
                instance_id = next(filter(lambda dimension: dimension['name'] == 'InstanceId', message["Trigger"]["Dimensions"]))["value"]
            except Exception as e:
                log.exception("Failed to get InstanceId from dimension %s : %s" % (message["Trigger"]["Dimensions"], e))
                continue


            now = misc.seconds_from_epoch_utc()

            response = self.context["dynamodb.client"].update_item(
                Key={
                    "AlarmName" : {'S': alarm_name}
                    },
                UpdateExpression="set InstanceId=:instanceid, %s_LastAlarmTimeStamp=:timestamp, %s_LastNewStateReason=:lastnewstatereason,"
                "%s_LastMetricName=:lastmetricname, %s_LastMetricNamespace=:lastmetricnamespace, "
                "%s_Event=:event,"
                "ExpirationTime=:expirationtime,"
                "LastRecordUpdateTime=:lastrecordupdatetime" % (new_state_value, new_state_value, new_state_value, new_state_value, new_state_value),
                ExpressionAttributeValues={
                   ':instanceid': {'S': instance_id},
                   ':timestamp': {'S': str(timestamp)},
                   ':lastnewstatereason': {'S': new_state_reason},
                   ':lastmetricname' : {'S': metric_name},
                   ':lastmetricnamespace': {'S': namespace},
                   ':event': {'S': json.dumps(message)},
                   ':expirationtime' : {'N': str(now + Cfg.get_duration_secs("snsmgr.record_expiration_delay"))},
                   ':lastrecordupdatetime' : {'N': str(now)}
                },
                ReturnConsumedCapacity='TOTAL',
                TableName=self.context["AlarmStateEC2Table"],
            )
            need_main_update = True

        if need_main_update:
            # Send a message to wakeup the Main Lambda function that is in
            #   charge to take appropriate decision
            sqs.call_me_back_send(self.ec2)
            log.debug("Sent SQS message to Main lambda queue: %s" % self.context["MainSQSQueue"])


