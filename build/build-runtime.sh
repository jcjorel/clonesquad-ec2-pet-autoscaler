#!/bin/bash -xe

export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone | sed 's/\(.*\)[a-z]/\1/')}

if [ -z "$GroupName" ]; then
	aws lambda get-function --function-name CloneSquad-Main-test >/tmp/main.json
	BASE_LAYER=$(jq -r '.["Code"]["Location"]' </tmp/main.json)

	LAYERS=
	OTHER_LAYER_ARNS=$(jq -r '.["Configuration"]["Layers"][].Arn' </tmp/main.json | tac)
	for arn in $OTHER_LAYER_ARNS ; do
		location=$(aws lambda get-layer-version-by-arn --arn $arn | jq -r '.["Content"]["Location"]')
		LAYERS="$LAYERS $location"
	done
fi
env

for layer in $LAYERS ; do
	curl $layer -o /tmp/package.zip 
	unzip /tmp/package.zip -d /opt
done

curl $BASE_LAYER -o /tmp/package.zip 
unzip /tmp/package.zip -d /var/task

rm -f /tmp/package.zip

