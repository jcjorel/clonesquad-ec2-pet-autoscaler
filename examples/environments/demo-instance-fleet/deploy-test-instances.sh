#!/bin/bash
set -e
if [ -z "${CLONESQUAD_DIR}" ] ; then
	echo "Please define CLONESQUAD_DIR environment variable!" ; exit 1
fi
source ${CLONESQUAD_DIR}/scripts/_source-me.sh

FLEET_SPECIFICATION=${FLEET_SPECIFICATION:-"t3.medium,Spot=True,Count=3;c5.large,Spot=True,Count=4;c5.large,Count=8;c5.xlarge,Count=5"}
STATIC_FLEET_SPECIFICATION=${STATIC_FLEET_SPECIFICATION:-"t3.micro,Spot=True,Count=2,SubFleetName=MySubfleet1;t3.micro,Count=2,SubFleetName=MySubfleet1;t3.micro,Count=2,SubFleetName=MySubfleet2"}
STATIC_FLEET_RDS_SPECIFICATION=${STATIC_FLEET_RDS_SPECIFICATION:-"aurora,Count=2,SubFleetName=MySubfleet3;mysql,Count=2,SubFleetName=MySubfleet1,Storage=10,DBClass=db.t3.micro"}

demo_run_dir=${CLONESQUAD_PARAMETER_DIR}/demo/instance-fleet
mkdir -p $demo_run_dir
TemplatefileName="$demo_run_dir/template-generated.yaml"
./generate-env.py --specs $FLEET_SPECIFICATION --subfleet-specs $STATIC_FLEET_SPECIFICATION \
	--subfleet-rds-specs $STATIC_FLEET_RDS_SPECIFICATION | tee $TemplatefileName

aws cloudformation deploy  --template-file $TemplatefileName --stack-name "CS-Demo-TestEC2nRDSInstances-$GroupName$1" --capabilities CAPABILITY_IAM \
	--parameter-overrides $(get_parameters) GroupName=$GroupName

cat <<EOF
Stack ready!

Optionaly, please consider activating Vertical Scaling with 'demo-loadbalancers' demonstration 
to experience smart management of instance types!

***** IMPORTANT *****
        # To activate Vertical Scaling copy/paste this:
        ${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${GroupName}-Configuration import <configure-ligthhouse-instance.yaml
***** IMPORTANT *****


EOF
