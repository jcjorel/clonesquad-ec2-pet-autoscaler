#!/bin/sh -e

# AWS_SSM_INSTANCE_ID

function cs_echo()
{
	type=$1 ; shift
	echo "CLONESQUAD-SSM-AGENT-$type:$SSM_COMMAND_ID:$*"
}

function run_user_scripts()
{
	subdir=$1
	(
		export CS_SSM_AGENT_SCRIPT_DIR=
		cd $CS_SSM_AGENT_SCRIPT_DIR
		for file in $subdir/*
		do
			if [ -f $file ] ; then
				cs_echo "USER-SCRIPT" "START"
				($file)
				cs_echo "USER-SCRIPT" "END"
			fi
		done
	)
}

function main()
{
	cs_echo "HELLO" "1.0"

	env | (while read LINE ; do cs_echo "ENV" "$LINE" ; done)

	CMD="##CMD##"
	case $CMD in
		INSTANCE_STATE_TRANSITION)
			cs_echo "$CMD" "START"
			run_user_scripts $1
			cs_echo "$CMD" "END"
		;;
		*)
			cs_echo "ERROR" "UNKOWN_COMMAND"
			exit 1
		;;
	esac
			

	cs_echo "BIE" ""
}

main
exit 0
