#!/bin/bash
# Startskript fuer den SIP-Translator
# CUDA 11.8 Library Path fuer faster-whisper/ctranslate2

VENV=/home/gh/python/venv_py311

export LD_LIBRARY_PATH=\
$VENV/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:\
$VENV/lib/python3.11/site-packages/nvidia/cublas/lib:\
$VENV/lib/python3.11/site-packages/nvidia/cudnn/lib:\
$LD_LIBRARY_PATH

exec $VENV/bin/python3 /home/gh/python/translator/translator.py "$@"
