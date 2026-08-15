#!/usr/bin/env bash
# Запуск STT в реальном времени.
# Выставляет LD_LIBRARY_PATH на cuBLAS/cuDNN из venv (нужно для CUDA в CTranslate2).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"
SITE="$DIR/.venv/lib/python3.14/site-packages"
export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$PY" -m stt_stream "$@"
