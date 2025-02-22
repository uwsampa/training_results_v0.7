#!/bin/bash

PGSYSTEM=${PGSYSTEM:-"PG"}
if [[ -f config_${PGSYSTEM}.sh ]]; then
  source config_${PGSYSTEM}.sh
else
  source config_PG.sh
  echo "Unknown system, assuming PG"
fi
SLURM_NTASKS_PER_NODE=${SLURM_NTASKS_PER_NODE:-$PGNGPU}
SLURM_JOB_ID=${SLURM_JOB_ID:-$RANDOM}
MULTI_NODE=${MULTI_NODE:-''}
echo "Run vars: id $SLURM_JOB_ID gpus $SLURM_NTASKS_PER_NODE mparams $MULTI_NODE"

# runs benchmark and reports time to convergence
# to use the script:
#   run_and_time.sh

set -e

# start timing
start=$(date +%s)
start_fmt=$(date +%Y-%m-%d\ %r)
echo "STARTING TIMING RUN AT $start_fmt"

# run benchmark
set -x

echo "running benchmark"

DATASET_DIR='/data'
[ ! -f /coco ] && ln -sf ${DATASET_DIR} /coco
echo `ls /data`

# run training
python -m bind_launch \
  --nsockets_per_node ${PGNSOCKET} \
  --ncores_per_socket ${PGSOCKETCORES} \
  --nproc_per_node $SLURM_NTASKS_PER_NODE $MULTI_NODE tools/train_mlperf.py \
  ${EXTRA_PARAMS} \
  --config-file 'configs/e2e_mask_rcnn_R_50_FPN_1x.yaml' \
  DTYPE 'float16' \
  PATHS_CATALOG 'maskrcnn_benchmark/config/paths_catalog_dbcluster.py' \
  MODEL.WEIGHT '/coco/models/R-50.pkl' \
  DISABLE_REDUCED_LOGGING True \
  "${EXTRA_CONFIG[@]}" ; ret_code=$?


set +x

sleep 3
if [[ $ret_code != 0 ]]; then exit $ret_code; fi

# end timing
end=$(date +%s)
end_fmt=$(date +%Y-%m-%d\ %r)
echo "ENDING TIMING RUN AT $end_fmt"

# report result
result=$(( $end - $start ))
result_name="OBJECT_DETECTION"

echo "RESULT,$result_name,,$result,Fujitsu,$start_fmt"

