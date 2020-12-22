# sam-sample-lambda

This project contains a very simple source code that acks every CloneSquad event sent to it.

This example can be customized to perform some business logic on event trigger.


## Deploy the sample application

To deploy this example, launch the following command **from the [DevKit](../../docs/BUILD_RELEASE_DEBUG.md#configuring-the-devkit-to-launch-demonstrations)**.

```bash
./deploy.sh
```

A new CloudFormation stack named 'sam-sample-clonesquad-notification-${GroupName}' is created that installs the example.

> To inform CloneSquad to send events to this Lambda function, fillin the Lambda function Arn in `UserNotificationArns` parameter of CloneSquad template and
update your stack.

## Cleanup

```bash
aws cloudformation delete-stack --stack-name sam-sample-clonesquad-notification-${GroupName}
```

