#!/bin/bash

if [ -z "${CLONESQUAD_DIR}" ] ; then
	        echo "Please define CLONESQUAD_DIR environmen variable!" ; exit 1
fi
source ${CLONESQUAD_DIR}/scripts/_source-me.sh

for PERIOD in 2 
do

	demoname="${PERIOD}hr-direct-config"
	./generate-env.py --period ${PERIOD} --demoname $demoname

	echo
	demoname="${PERIOD}hr-with-parameterset-config"
	./generate-env.py --period ${PERIOD} --demoname $demoname --with-parameterset True

	echo
	echo "rds.enable: 1" | ${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${STACK_NAME}-Configuration import --ttl=days=1
	${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${STACK_NAME}-Scheduler import --ttl=days=1 <subfleet-hourly-flipflop-cronfile.yaml
	echo "Configured subfleets Flip-Flop scheduling demo!!"
	echo

	for variant in "min_instance_count" "desired_instance_count"
	do
		echo "Next step: Inject demo variant writing configuration variables directly"
		echo "# ${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${STACK_NAME}-Scheduler import --ttl=days=1 <$demoname-$variant-cronfile.yaml"
		echo "Next step: Inject demo variant using parameterset indirection (scheduler and configuration to modify)"
		echo "# ${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${STACK_NAME}-Configuration import --ttl=days=1 <$demoname-$variant-configfile.yaml"
		echo "# ${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${STACK_NAME}-Scheduler import --ttl=days=1 <$demoname-$variant-cronfile.yaml"
	done
done

echo "Note: --ttl parameter defines that demo will automatically expires after one day!"
