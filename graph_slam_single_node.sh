#/bin/bash

export GLOG_minloglevel=2
export MAGNUM_LOG=quiet

set -x
python -u -m torch.distributed.launch \
    --use_env \
    --nproc_per_node 1 \
    habitat_baselines/run.py \
    --exp-config habitat_baselines/config/objectnav/graph_ddppo_slam_objectnav.yaml \
    --run-type eval

