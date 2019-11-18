#!/usr/bin/env bash

resume=$1
algorithm=$2
aux="${@:3}"

echo resume $1 algorithm $2

loc=`dirname "%0"`

case "$algorithm" in
    ("bbo") args="--algorithm=$algorithm --game=RUN --budget=1000 --explore=grad_direct --delta=0.1 --epsilon=0.1";;
    ("grad") args="--grad --algorithm=$algorithm --game=RUN --budget=1000 --explore=grad_direct --delta=0.01 --epsilon=0.01";;
    (*) echo "$algorithm: Not Implemented" ;;
esac

CUDA_VISIBLE_DEVICES=1, python main.py --identifier=debug2 --action-space=2 --resume=$resume --load-last-model $args $aux &
CUDA_VISIBLE_DEVICES=2, python main.py --identifier=debug3 --action-space=3 --resume=$resume --load-last-model $args $aux &

CUDA_VISIBLE_DEVICES=0, python main.py --identifier=debug5 --action-space=5 --resume=$resume --load-last-model $args $aux &
CUDA_VISIBLE_DEVICES=0, python main.py --identifier=debug10 --action-space=10 --resume=$resume --load-last-model $args $aux &

CUDA_VISIBLE_DEVICES=2, python main.py --identifier=debug20 --action-space=20 --resume=$resume --load-last-model $args $aux &
CUDA_VISIBLE_DEVICES=1, python main.py --identifier=debug40 --action-space=40 --resume=$resume --load-last-model $args $aux &

CUDA_VISIBLE_DEVICES=1, python main.py --identifier=vae --action-space=784 --budget=100 --vae --resume=$resume --load-last-model $args $aux &