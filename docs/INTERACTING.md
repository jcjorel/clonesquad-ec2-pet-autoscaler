
# Interacting with CloneSquad

CloneSquad comes with a minimalistic Interaction API that is going to evolve overtime.

There are 2 ways to interact with a CloneSquad deployment:
* A public API Gateway,
* An SQS Queue.

Only the API Gateway is able to reply back an answer. The SQS queue can only be used to send command in an asynchronous manner
without reply channel.

These 2 resources can identified from the CloudFormation outputs or dynamically with a dedicated Lambda discovery function.

Ex: 
```shell
tmpfile=/tmp/cs-config.$$
aws lambda invoke --function-name CloneSquad-Discovery-${GroupName} --payload '' $tmpfile 1>/dev/stderr
APIGW_URL=$(jq -r '.["InteractAPIGWUrl"]' <$tmpfile)
rm -f $tmpfile
```

## SQS usage and message payload format

The SQS queue is protected by a security policy requiring that all allowed senders be listed in the `UserNotificationArns` Cloudformation template parameter.   
Note: This parameter can contain wildcards ("*" and "?")

```json
	{
		"OpType": "<Interact_API_operation>",
		...
			<<Other operation specific parameters>>
		...
	}
```

## API Gateway usage

Url format: https://<api_gateway_hostname>/v1/*<Interact_API_operation>*

If an operation takes parameters, they have to be sent with the same format than the SQS payload in a POST request.

The API gateway requires SiGV4 authentication by default so you must present valid STS credentials to get access.
Using a tool like 'awscurl' can simplify this process or other AWS SDK managing as well this kind of authentication.


# Interaction API operations

## Notify/AckEvent


