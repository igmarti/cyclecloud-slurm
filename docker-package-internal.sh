#!/usr/env/bin bash
## Called inside the container
set -e

yum install -y python36 wget

/source/azure-slurm/build.sh /source/cyclecloud-scalelib