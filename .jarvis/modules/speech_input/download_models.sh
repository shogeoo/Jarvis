#!/usr/bin/env bash
# Скачивание моделей для Jarvis.
#   ./download_models.sh              # faster-whisper large-v3-turbo в HF-кэш
#   HF_ENDPOINT=https://hf-mirror.com ./download_models.sh  # через зеркало
#   ./download_models.sh --tiny       # маленькая модель для проверки
set -euo pipefail
MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$MODULE_DIR/../../.." && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
REPO="${REPO:-mobiuslabsgmbh/faster-whisper-large-v3-turbo}"
MODEL="${1:-large-v3-turbo}"

case "$MODEL" in
  tiny) REPO="Systran/faster-whisper-tiny" ;;
  base) REPO="Systran/faster-whisper-base" ;;
  small) REPO="Systran/faster-whisper-small" ;;
  medium) REPO="Systran/faster-whisper-medium" ;;
  large-v3-turbo) REPO="mobiuslabsgmbh/faster-whisper-large-v3-turbo" ;;
  large-v3 | large) REPO="Systran/faster-whisper-large-v3" ;;
  *) echo "Неизвестная модель: $MODEL"; exit 1 ;;
esac

echo "==> Скачивание $REPO в HF-кэш (~/.cache/huggingface)"
echo "==> HF_ENDPOINT=${HF_ENDPOINT:-<default>}"
echo "==> Логи ниже показывают прогресс по файлам (tqdm)."
echo

"$PY" - "$REPO" <<'PYEOF'
import sys
from huggingface_hub import snapshot_download

repo = sys.argv[1]
path = snapshot_download(
    repo,
    allow_patterns=["*.bin", "*.json", "*.txt", "*.py"],
)
print()
print(f"OK: модель в {path}")
PYEOF

echo
echo "==> Скачивание silero_vad.onnx"
VAD="$PROJECT_DIR/.jarvis/runtime/models/silero_vad.onnx"
mkdir -p "$(dirname "$VAD")"
curl -fL -o "$VAD" \
  "https://huggingface.co/onnx-community/silero-vad/resolve/main/onnx/model.onnx"
echo "OK: $VAD ($(du -h "$VAD" | cut -f1))"
