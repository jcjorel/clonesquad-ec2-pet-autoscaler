#!/bin/bash

alarm_names=$(aws cloudwatch describe-alarms --alarm-name-prefix CloneSquad-${GroupName}-i- | grep AlarmName | tr '":, ' '   ' | awk '{print $2;}' | sort -R)
first_alarm_name=$(echo $alarm_names | awk '{print $1;}')
echo $first_alarm_name
set -x
aws cloudwatch set-alarm-state --alarm-name $first_alarm_name --state-value ${1:-ALARM} --state-reason "$0"
