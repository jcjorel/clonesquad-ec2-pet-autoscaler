#!/bin/sh -e

# AWS_SSM_INSTANCE_ID
# SSM_COMMAND_ID
export CS_SSM_AGENT_SCRIPT_DIR=/etc/cs-ssm/

function cs_echo()
{
	type=$1 ; shift
	echo "CLONESQUAD-SSM-AGENT-$type:$*"
}

function run_user_scripts()
{
	subdir=$CS_SSM_AGENT_SCRIPT_DIR/$1
	shift
	if [ -d $subdir ] ; then
		(
			cd $subdir
			for file in $subdir/*
			do
				if [ -x $file ] ; then
					cs_echo "USER-SCRIPT" "START:$*"
					(set +e ; $file $*)
					cs_echo "USER-SCRIPT" "END"
				elif [ -f $file ] ; then
					cs_echo "USER-SCRIPT" "WARNING:SCRIPT_PERMISSION:$file"
					cs_echo "DETAILS" "File $file is not executable!"
				fi
			done
		)
	fi
	cs_echo "STATUS" "SUCCESS"
}

function probe_test()
{
	probe=$1
	ok_msg=$2
	nok_msg=$3
	cmd="$CS_SSM_AGENT_SCRIPT_DIR/$probe"
	if [ -x $cmd ]; then
		if $cmd ; then
			cs_echo "STATUS" "SUCCESS"
			cs_echo "DETAILS" "$cmd returned that $ok_msg."
		else
			cs_echo "STATUS" "FAILED"
			cs_echo "DETAILS" "$cmd returned that $nok_msg"
		fi
	elif [ -e $cmd ]; then
		cs_echo "STATUS" "FAILED"
		cs_echo "DETAILS" "$cmd exists on instance but is not executable: $nok_msg!"
	else
		cs_echo "STATUS" "SUCCESS"
	fi
}

function main()
{
	cs_echo "HELLO" "1.0"

	#env | (while read LINE ; do cs_echo "ENV" "$LINE" ; done)
	if ! [ -d $CS_SSM_AGENT_SCRIPT_DIR ] ; then
		cs_echo "MAIN" "WARNING:MISSING_AGENT_DIR:$CS_SSM_AGENT_SCRIPT_DIR"
		cs_echo "DETAILS" "Directory $CS_SSM_AGENT_SCRIPT_DIR doesn't exist on instance $AWS_SSM_INSTANCE_ID!"
	fi

	CMD=$1
	shift
	cs_echo "$CMD" "START:$*"
	case $CMD in
	INSTANCE_STATE_TRANSITION)
		run_user_scripts instance-state-transition $*
	;;
	INSTANCE_HEALTHCHECK)
		probe_test instance-healthcheck \
			"instance $AWS_SSM_INSTANCE_ID is HEALTHY!" "instance $AWS_SSM_INSTANCE_ID is UNHEALTHY!"
	;;
	INSTANCE_READY_FOR_OPERATION)
		probe_test instance-ready-for-operation \
			"instance $AWS_SSM_INSTANCE_ID is READY!" "instance $AWS_SSM_INSTANCE_ID is NOT yet ready!"
	;;
	INSTANCE_READY_FOR_SHUTDOWN)
		probe_test instance-ready-for-shutdown \
			"instance $AWS_SSM_INSTANCE_ID is ready for shutdown!" "instance $AWS_SSM_INSTANCE_ID is NOT ready for shutdown!"
	;;
	*)
		cs_echo "STATUS" "ERROR:UNKOWN_COMMAND"
	;;
	esac
	cs_echo "$CMD" "END"

	cs_echo "BIE" ""
}

main ##CMD## ##ARGS##
exit 0
