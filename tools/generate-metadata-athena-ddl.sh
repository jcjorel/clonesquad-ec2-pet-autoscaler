#!/bin/bash -e

S3_URL=$1

if [ -z "$S3_URL" ] ; then
	echo "Usage: $0 <S3 location>" ; exit 1
fi

tmpdir=~/cs-meta
mkdir -p $tmpdir/metadata
aws s3 sync $S3_URL $tmpdir/metadata
launchdir=$(pwd)
seconds=$(date +%s)
mkdir -p ${launchdir}/docs/metadata
cd $tmpdir/metadata
for table in * ; do
	mkdir -p $table/archive/$seconds
	mv $table/* $table/archive/$seconds 2>/dev/null || true
done
for table in * ; do
	(	echo "--WARNING: In the AWS Athena console, paste only one SQL statement at a time!" 
		${launchdir}/tools/quick-and-dirty-aws-athena-ddl-generator-for-json `find $table -name '*.json' -print` \
			--table-name "clonesquad_${table}" --partitioned-by "PARTITIONED BY (accountid string, region string, groupname string)" 
		echo "--After table creation, please run below SQL statement after each CloneSquad deployment."
		echo "MSCK REPAIR TABLE clonesquad_${table};" ) >${launchdir}/docs/metadata/clonesquad_${table}.ddl 
	${launchdir}/tools/quick-and-dirty-aws-athena-ddl-generator-for-json `find $table -name '*.json' -print` \
		--table-name "clonesquad_${table}" --partitioned-by "PARTITIONED BY (accountid string, region string, groupname string)" \
		--location ${S3_URL}/${table}
	echo "--After table creation, please run below SQL statement after each CloneSquad deployment."
	echo "MSCK REPAIR TABLE clonesquad_${table};" 
done
