#!/bin/bash
set -e

if [ -z "$CLONESQUAD_DIR" ]; then
	echo "Please define CLONESQUAD_DIR variable!" ; exit 1
fi
source ${CLONESQUAD_DIR}/scripts/_source-me.sh

if ! [ -e $CLONESQUAD_PARAMETER_DIR/samconfig.toml ] ; then
	HERE=$PWD
	echo "Please run once '(cd $CLONESQUAD_PARAMETER_DIR ; sam deploy --guided -t $HERE/templace.yaml)'!" ; exit 1
fi

export $(grep -v '\[' $CLONESQUAD_PARAMETER_DIR/samconfig.toml | sed 's/ = /=/g' | xargs)

echo "Deploying stack $STACK_NAME..."

build_and_deploy()
{
	set -x
	set -e
	sam build && 
	sam deploy --no-confirm-changeset \
		--stack-name=sam-sample-clonesquad-notification-${GroupName} \
		--s3-bucket=$s3_bucket --s3-prefix=$s3_prefix --region=$AWS_DEFAULT_REGION --capabilities CAPABILITY_IAM
}
time build_and_deploy
