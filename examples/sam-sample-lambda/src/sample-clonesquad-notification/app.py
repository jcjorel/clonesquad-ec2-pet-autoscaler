import json
import boto3

def lambda_handler(event, context):
    """Sample Lambda function reacting to CloneSquad events

    Parameters
    ----------
    event: dict, required


    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------
    """

    print("Event:")
    print(json.dumps(event, default=str))

    # Acknowledge the events to avoid be called back again
    sqs_client  = boto3.client("sqs")
    ack_sqs_url = event["Metadata"]["AckSQSUrl"]

    for event in event["Events"]:
        event_date = event["EventDate"]

        # Do business logic here

        # Publish to the notification SQS queue to ack this event.
        #   Note: Multiple events could be acked together in a single 
        #         push if needed/prefered
        payload = json.dumps({
                    "OpType" : "Notify/AckEvent",
                    "Events" : [event_date]})

        print("Sending to SQS queue '%s' for Event '%s'..." % (ack_sqs_url, event_date))
        response = sqs_client.send_message(
            QueueUrl=ack_sqs_url,
            MessageBody=payload)


