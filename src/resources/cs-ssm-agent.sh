#!/bin/sh -e

# AWS_SSM_INSTANCE_ID
# SSM_COMMAND_ID
export CS_SSM_AGENT_SCRIPT_DIR=/etc/cs-ssm/
export CS_GW_URL="##ApiGwUrl##"
echo "Info: CloneSquad API GW Url is $CS_GW_URL (Available in \$CS_GW_URL environment variable)."

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

function block_new_connections_to_ports()
{
	blocked_ports=$*
	cs_echo "BLOCK_NEW_CONNECTIONS" "PORTS:$blocked_ports"
	if ! which iptables-save || ! which iptables ; then
		cs_echo "BLOCK_NEW_CONNECTIONS" 'WARNING:Can not find iptables-save or iptables in SSM Agent $PATH'
		cs_echo "BLOCK_NEW_CONNECTIONS" "END"
		return 0
	fi
	# Execute only once
	chain="CS-AGENT"
	if ! [ -z "$(sudo iptables-save | grep $chain)" ] ; then
		cs_echo "BLOCK_NEW_CONNECTIONS" "ALREADY_DONE:IPtable already modified."
		cs_echo "BLOCK_NEW_CONNECTIONS" "END"
		return 0
	fi
	# Install an IPtable that is blocking any new TCP connection
	echo "Creating CloneSquad agent dedicated IPtable chain '$chain'..."
	sudo iptables -N $chain 
	# Check if we need to use extra iptables parameters
	parameter_file=/etc/cs-ssm/blocked-connections/extra-iptables-parameters.txt
	if [ -e $parameter_file ] ; then
		extra_parameters=$(cat $parameter_file)
		echo "Found IPTables extra parameters in $parameter_file : $extra_parameters"
	else
		echo "No extra parameter file $parameter_file on instance..."
	fi
	# Insert the chain in front of all rules
	sudo iptables -I INPUT -j $chain 
	for port in $blocked_ports ; do
		echo "Blocking new connections to TCP port $port..."
		sudo iptables -A $chain -p tcp -m tcp --dport $port -m state $extra_parameters \
			--state NEW -j REJECT --reject-with icmp-port-unreachable ||
			cs_echo "BLOCK_NEW_CONNECTIONS" "WARNING:Failed to install IPTable rule for port '$port'!"
	done
	# Output in logs the changes
	cs_echo "BLOCK_NEW_CONNECTIONS" "IPTABLES-OUTPUT"
	sudo iptables-save
	cs_echo "BLOCK_NEW_CONNECTIONS" "END"
	return 0
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
	INSTANCE_BLOCK_NEW_CONNECTIONS_TO_PORTS)
		block_new_connections_to_ports ##BlockedPorts##
	;;
	INSTANCE_SCALING_STATE_DRAINING | INSTANCE_SCALING_STATE_BOUNCED)
		new_state="##NewState##"
		old_state="##OldState##"
		cs_echo $CMD "$new_state $old_state"
		probe_test instance-scaling-state-change-$new_state  $old_state \
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
