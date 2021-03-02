
# Event Scheduler

CloneSquad has an integrated time scheduling module leveraging, behind the scene, CloudWatch Event rules.
Each item of the DynamoDB table *'CloneSquad-{GroupName}-Scheduler'* will be translated in a CloudWatch Event rule.

Actions linked to an event are configuration parameter settings in the *'CloneSquad-{GroupName}-Configuration'* DynamoDB table.

An example of a "complex" scaling using the Event scheduler in located in [examples/environments/demo-scheduled-events/](../examples/environments/demo-scheduled-events/). It implements a schedule plan over 2 hours that starts and stops instances to draw a Sinus wave on the Cloudwatch dashboard.

Configuration of an event in the DynamoDB table has the following format: The Key is a uniq name and value is a [MetaString](CONFIGURATION_REFERENCE.md#MetaString).

The event scheduler uses CloudWatch rules behing the scene, so time and dates and expressed in UTC.

	| Key            | Value                                                                    |
	|----------------|--------------------------------------------------------------------------|
	| <event_name1>  | cron(0 * * * ? *),config.active_parameter_set=test-pset                  |
	| <event_name2>  | cron(0 6\\,18 0 * ? *),ec2.schedule.min_instance_count=3,ec2.other_key=4 |

Note: Coma needs to be double-escaped with backslashes.

## Working with local timezone cron entries

If you do not want to work with UTC based time, the event scheduler proposes the `localcron` keyword that replaces the `cron` one.
Internally, the [`arrow` library](https://arrow.readthedocs.io/en/latest/) is used which is DST (Daylight Saving Time) aware: So, DST should
be handled transparently. 

	| Key                     | Value                                                           |
	|-------------------------|-----------------------------------------------------------------|
	| <local_tz_event_name1>  | localcron(0 12 * * ? *),ec2.schedule.min_instance_count=3       |

CloudWatch Event rules are still used internally so the event scheduler will attempt to translate local time specification into an UTC based one.
This is working well when minutes and hours are not wildcard based. When wildcards are used (ex: `*/10`), they won't be translated. As consequence, 
some imprecisions could occur. **When wildcards are used, please always check the generated UTC time base cron rule in CloudWatch event to check it is what you want.**

> **The local time zone is [guessed from the AWS region](../src/resources/region-timezones.yaml) where CloneSquad is deployed and can be overriden with 
the [TimeZone deployment parameter](DEPLOYMENT_REFERENCE.md#timezone).**

## CloudWatch Limitations about cron scheduling expression

As the CloneSquad scheduler uses CloudWatch Event, it shares the same limitations.

> **You can't specify the Day-of-month and Day-of-week fields in the same cron expression. If you specify a value (or a '*') in one of the fields, you must use a ? (question mark) in the other.**



