#!/bin/bash
#
# This is really a very dumb ALB load generator! Wants to contribute something smarter => Please feel free!

if [ -z "$1" ]; then
	echo "Usage: $0 <loadbalancerurl>" ; exit 1
fi
URL=$1
ITERATIONS=${ITERATIONS:-100000}
${CLONESQUAD_DIR}/.venv/bin/python3 ./load-generator.py http://${URL}/cgi-bin/cpu?$ITERATIONS
