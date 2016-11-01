#!/bin/bash
set -euo pipefail

# Sets up travis-ci environment for testing bioconda-utils.

if [[ $TRAVIS_OS_NAME = "linux" ]]
then
    tag=Linux
else
    tag=MacOSX
fi
curl -O https://repo.continuum.io/miniconda/Miniconda3-latest-${tag}-x86_64.sh
sudo bash Miniconda3-latest-${tag}-x86_64.sh -b -p /anaconda
sudo chown -R $USER /anaconda
export PATH=/anaconda/bin:$PATH

# Add channels in the specified order.
for channel in $(grep -v "^#" bioconda_utils/channel_order.txt); do
    conda config --add channels $channel
done

conda config --get
conda install -y --file conda-requirements.txt

python setup.py install

pip install -r pip-test-requirements.txt
pip install -r pip-requirements.txt

# Add local channel as highest priority
conda config --add channels file://anaconda/conda-bld
conda config --get

# involucro used for mulled-build
curl -O https://github.com/involucro/involucro/releases/download/v1.1.2/involucro
sudo mv involucro /opt/involucro
sudo chmod +x /opt/involucro
export PATH=/opt/involucro:$PATH