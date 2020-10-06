# sam-sample-lambda

This project contains a very simple source code that acks every CloneSquad event sent to it.


## Deploy the sample application

To deploy this example, launch the following command **from the [DevKit](../../docs/BUILD_RELEASE_DEBUG.md#configuring-the-devkit-to-launch-demonstrations)**.

```bash
./deploy.sh
```

A new CloudFormation stack named 'sam-sample-clonesquad-notification-${GroupName}' is created that installs the example.

> To inform CloneSquad to send events to this Lambda function, execute again the configuration wizard 'cs-deployment-configuration-wizard'. It
will automatically detect the function based on the CloudFormation template name.

## Cleanup

```bash
aws cloudformation delete-stack --stack-name sam-sample-clonesquad-notification-${GroupName}
```

