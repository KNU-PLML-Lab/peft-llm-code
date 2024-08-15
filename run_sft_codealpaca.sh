#!/usr/bin/env bash

export WANDB_PROJECT="PEFT-Study"

run_name="phi-3-mini-128k-instruct_lora_codealpaca_5e-5"

CUDA_VISIBLE_DEVICES=1 python sft.py \
  --output_dir "runs/${run_name}" \
  --model_name_or_path "Microsoft/Phi-3-mini-128k-instruct" \
  --trust_remote_code \
  --dataset_name datasets/codealpaca \
  --torch_dtype bfloat16 \
  --per_device_train_batch_size 8 \
  --per_device_eval_batch_size 4 \
  --gradient_accumulation_steps 4 \
  --num_train_epochs 5 \
  --learning_rate 5e-5 \
  --max_seq_length 512 \
  --gradient_checkpointing \
  --optim adafactor \
  --use_peft \
  --lora_target_modules o_proj, qkv_proj \
  --completion_only \
  --response_template "<|assistant|>" \
  --eval_strategy "epoch" \
  --logging_steps 1 \
  --save_strategy "epoch" \
  --report_to "wandb" \
  --run_name "${run_name}" \
  --lora_r 16 \
  --lora_alpha 32
