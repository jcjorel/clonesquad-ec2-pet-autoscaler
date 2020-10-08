#!/bin/bash 

if [ -z "${CLONESQUAD_DIR}" ] ; then
	echo "Please define CLONESQUAD_DIR environment variable!" ; exit 1
fi
source ${CLONESQUAD_DIR}/scripts/_source-me.sh

TemplatefileName="template-generated.yaml"

# 2 ALBs and 1 NLB
specs="1,port=80,protocol=HTTP;2,port=80,protocol=HTTP;3,port=22,protocol=TCP"

# 1 ALB (faster to provision but also CloneSquad is easier to debug as everything 
#    is faster with a single target group to manage (faster registration,deregistration, etc...)
#specs="1,port=80,protocol=HTTP"

./generate-env.py $specs >$TemplatefileName

STACK_NAME="CS-Demo-LoadBalancers-$GroupName$VariantNumber"
aws cloudformation deploy  --template-file $TemplatefileName --stack-name $STACK_NAME \
	--parameter-overrides $(get_parameters)

echo "LoadBalancer DNS names:"
aws cloudformation describe-stacks --stack-name $STACK_NAME --output text | grep 'DNS' | awk '{print $7;}'

# Configure CloneSquad to track the alarm defined in the CloudFormation template
sed "s/-{GroupName}-/-${GroupName}-/g" <configure-lb-responsetime-alarm.yaml | ${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-$GroupName-Configuration import 
