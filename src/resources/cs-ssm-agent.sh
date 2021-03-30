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
	if ! [ -d $CS_SSM_AGENT_SCRIPT_DIR ] ; then
		cs_echo "USER-SCRIPT" "WARNING:MISSING_AGENT_DIR:$CS_SSM_AGENT_SCRIPT_DIR"
		cs_echo "DETAILS" "Directory $CS_SSM_AGENT_SCRIPT_DIR doesn't exist on instance $AWS_SSM_INSTANCE_ID!"
		return
	fi
	subdir=$1
	(
		cd $CS_SSM_AGENT_SCRIPT_DIR
		for file in $subdir/*
		do
			if [ -f $file ] ; then
				cs_echo "USER-SCRIPT" "START"
				(set +e ; $file)
				cs_echo "USER-SCRIPT" "END"
			fi
		done
	)
}

function main()
{
	cs_echo "HELLO" "1.0"

	#env | (while read LINE ; do cs_echo "ENV" "$LINE" ; done)

	CMD=$1
	case $CMD in
		INSTANCE_STATE_TRANSITION)
			cs_echo "$CMD" "START"
			run_user_scripts $*
			cs_echo "$CMD" "END"
			cs_echo "STATUS" "SUCCESS"
		;;
		*)
			cs_echo "STATUS" "ERROR:UNKOWN_COMMAND"
		;;
	esac
			

	cs_echo "BIE" ""
}

main ##CMD## ##ARGS##
exit 0
