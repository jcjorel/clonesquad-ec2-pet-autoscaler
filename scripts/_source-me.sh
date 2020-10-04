#!/bin/bash

if [ -z "$CLONESQUAD_PARAMETER_DIR" ]; then
	echo "[ERROR] please set CLONESQUAD_PARAMETER_DIR environment variable!" ; exit 1  
fi

export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone | sed 's/\(.*\)[a-z]/\1/')}
export ACCOUNT_ID=${ACCOUNT_ID:-$(aws sts get-caller-identity | jq -r '.["Account"]')}
export AWS_ACCOUNT_ID=$ACCOUNT_ID

if [ -e ${CLONESQUAD_PARAMETER_DIR}/deployment-parameters.txt ] ; then
	export $(grep -v '#.*' ${CLONESQUAD_PARAMETER_DIR}/deployment-parameters.txt | xargs)
fi

get_parameters()
{
	grep -v '#.*' ${CLONESQUAD_PARAMETER_DIR}/deployment-parameters.txt | tr '\n' ' '
}
export STACK_NAME=$GroupName
