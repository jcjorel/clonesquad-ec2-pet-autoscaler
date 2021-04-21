#!/bin/bash -e

S3_URL=$1

if [ -z "$S3_URL" ] ; then
	echo "Usage: $0 <S3 location>" ; exit 1
fi

tmpdir=$(mktemp -d)
mkdir -p $tmpdir/metadata
aws s3 sync $S3_URL $tmpdir/metadata
launchdir=$(pwd)
mkdir -p ${launchdir}/docs/metadata
cd $tmpdir/metadata
for table in * ; do
	(${launchdir}/tools/quick-and-dirty-aws-athena-ddl-generator-for-json `find $table -name '*.json' -print` \
		--table-name "clonesquad_${table}" --partitioned-by "PARTITIONED BY (accountid string, region string, groupname string)" ;
			echo "--MSCK REPAIR TABLE clonesquad_${table};" ) >${launchdir}/docs/metadata/clonesquad_${table}.ddl 
	${launchdir}/tools/quick-and-dirty-aws-athena-ddl-generator-for-json `find $table -name '*.json' -print` \
		--table-name "clonesquad_${table}" --partitioned-by "PARTITIONED BY (accountid string, region string, groupname string)" \
		--location ${S3_URL}/${table}
	echo "MSCK REPAIR TABLE clonesquad_${table};" 
done
rm -fr $tmpdir
