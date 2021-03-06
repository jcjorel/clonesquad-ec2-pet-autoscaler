import os
import json
import gzip
import base64
import boto3

def manage_event(event_date, event_type, input_data, meta_data):
    """ Manage an event sent by CloneSquad.

    >- CUSTOMIZE THIS FUNCTION TO ADD YOUR BUSINESS LOGIC -<

    :param event_type
    :param input_data
    :param meta_data

    :return True if the event can be acked. False if CloneSquad needs to send again this event (retry).
    """
    print(f"Date: {event_date}, Event: {event_type}, InputData: {input_data}")
    # The instance ids associated with the current event
    # Note: Not all events have the InstanceIds parameter
    instance_ids           = input_data['**kwargs'].get("InstanceIds", [])
    # Instance structures of all instances managed by CloneSquad (ec2.describe_instances() output)
    describe_instance_data = meta_data.get("EC2", {}).get("AllInstanceDetails", [])
    # The instance structures involved in this event
    instances              = [i for i in describe_instance_data if i["InstanceId"] in instance_ids]

    ####################################
    # Put your business logic here !!!
    ####################################

    # DEMO - DEMO - DEMO - DELETE ME!
    for instance in instances:
        instance_id = instance["InstanceId"]
        if event_type in ["start_instances", "drain_instances", "stop_instances"]:
            print(f"DEMO - Received event {event_type} for instance id {instance_id}!")
            # Parse instance tags to display the name of the instance (if any)
            instance_name_tag = next(filter(lambda t: t["Key"] == "Name", instance["Tags"]), None)
            if instance_name_tag is not None:
                instance_name = instance_name_tag["Value"]
                print(f"Instance name for '{instance_id}' : {instance_name}")
            # Display all the Tags of each instance
            print("DEMO - %s : Tags=%s" % (instance_id, instance["Tags"]))
    # DEMO - DEMO - DEMO - DELETE ME!
    return True



def lambda_handler(event, context):
    """ Sample Lambda function reacting to CloneSquad events sent from a Lambda invoke or a SQS trigger
    """

    #print("Event:")
    #print(json.dumps(event, default=str))

    notifications = []
    if "Records" in event:
        for r in event["Records"]:
            if r.get("eventSource") != "aws:sqs":
                continue
            print("Triggered from an SQS queue %s" %  r["eventSourceARN"])
            e = json.loads(r["body"])
            e["_sqs.messageId"]      = r["messageId"] 
            e["_sqs.eventSourceARN"] = r["eventSourceARN"] 
            e["_sqs.receiptHandle"]  = r["receiptHandle"] # Keep track of receipt handle to delete the message
            notifications.append(e)
    else:
        notifications.append(event) # Called directly from a Lambda invoke

    sqs_client  = boto3.client("sqs")
    ack_sqs_url = None
    for notif in notifications:
        if "Metadata" in notif and "AckSQSUrl" in notif["Metadata"]:
            # The SQS Url to acknowledge the events and avoid be called back again
            ack_sqs_url = notif["Metadata"]["AckSQSUrl"]
        else:
            print("[ERROR] No 'AckSQSUrl' in event!! (???)")


        metadata = {}
        for e in notif["Events"]:
            event_date = e["EventDate"]

            if "Metadata" in e:
                # Gunzip the Metadata field if present in the event
                #   Note: Metadata field is only present if it has a different value than the previous event
                uncompressed_metadata = str(gzip.decompress(base64.b64decode(e["Metadata"])), "utf-8")
                #print("Metadata content: (512 first bytes...)")
                #print(uncompressed_metadata[:512])
                #print(f"Metadata for the event '{event_date} is %d bytes long." % len(uncompressed_metadata))
                metadata              = json.loads(uncompressed_metadata)
            event_type = e["EventType"]
            input_data = json.loads(e["InputData"])
            #print(f"Received event {event_date} - {event_type} - {input_data}")

            if not manage_event(event_date, event_type, input_data, metadata):
                continue # Do not ack event


            if ack_sqs_url is not None:
                # Publish to the notification SQS queue to ack this event.
                #   Note: Multiple events could be acked together in a single 
                #         push if needed/prefered
                payload = json.dumps({
                            "OpType" : "Notify/AckEvent",
                            "Events" : [event_date]})

                print("Sending EventAck to SQS queue '%s' for event '%s'..." % (ack_sqs_url, event_date))
                response = sqs_client.send_message(
                    QueueUrl=ack_sqs_url,
                    MessageBody=payload)

        # If event received from an SQS queue, delete the message
        if "_sqs.receiptHandle" in notif:
            m_h        = notif["_sqs.receiptHandle"]
            m_id       = notif["_sqs.messageId"]
            queue_arn  = notif["_sqs.eventSourceARN"]
            queue_name = queue_arn.split(':')[-1]
            account_id = queue_arn.split(':')[-2]
            print(f"Delete SQS message {queue_name}/{account_id} from {queue_arn}...")
            try:
                response = sqs_client.get_queue_url(
                   QueueName=queue_name,
                   QueueOwnerAWSAccountId=account_id
                   )
                sqs_client.delete_message(QueueUrl=response["QueueUrl"],
                   ReceiptHandle=m_h)
            except Exception as e:
                print(f"Got Exception while deleting SQS message! {e}")


