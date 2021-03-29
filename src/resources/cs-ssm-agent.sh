#!/bin/bash -e

function cs_echo()
{
	type=$1 ; shift
	echo "CLONESQUAD-SSM-AGENT-$type:$*"
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

cs_echo "HELLO" "1.0"

env | (while read LINE ; do cs_echo "ENV" "$LINE")

CMD=$1
shift
case $CMD in
	INSTANCE_STATE_TRANSITION)
		run_user_scripts $1
	;;
	*)
		cs_echo "ERROR" "UNKOWN_COMMAND"
	;;
esac
		

cs_echo "BIE"
exit 0
