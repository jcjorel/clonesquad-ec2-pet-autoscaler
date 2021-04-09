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

function block_new_connections_to_port()
{
	blocked_ports=$*
	cs_echo "BLOCK_NEW_CONNECTIONS" "PORTS:$blocked_ports"
	cs_echo "BLOCK_NEW_CONNECTIONS" "END"
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
	ENTER_MAINTENANCE_WINDOW_PERIOD)
		probe_test enter-maintenance-window-period \
			"instance $AWS_SSM_INSTANCE_ID acked the event!" \
			"instance $AWS_SSM_INSTANCE_ID returned a non-zero code. The message will be repeated..."
	;;
	EXIT_MAINTENANCE_WINDOW_PERIOD)
		probe_test exit-maintenance-window-period \
			"instance $AWS_SSM_INSTANCE_ID acked the event!" \
			"instance $AWS_SSM_INSTANCE_ID returned a non-zero code. The message will be repeated..."
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
	INSTANCE_SCALING_STATE_DRAINING | INSTANCE_SCALING_STATE_BOUNCED | INSTANCE_SCALING_STATE_NONE)
		new_state="##NewStates##"
		old_state="##OldStates##"
		if [ "$new_state" == "draining" ] ; then
			block_new_connections_to_port ##BlockedPorts##
		fi
		probe_test instance-scaling-state-change $new_state  $old_state \
			"instance $AWS_SSM_INSTANCE_ID has acked the '$new_state' state (Previous state was $old_state)." \
			"instance $AWS_SSM_INSTANCE_ID did not acked the '$new_state' state (Previous state was $old_state). Message will be repeated!"
	;;
	*)
		cs_echo "STATUS" "ERROR:UNKOWN_COMMAND"
	;;
	esac
	cs_echo "$CMD" "END"

	cs_echo "BIE" ""
}

main ##Cmd## ##Args##
exit 0
