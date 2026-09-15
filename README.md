# Jarvis — STT в реальном времени

Локальное распознавание речи (русский/английский) с нарезкой на аудио-чанки
по паузам. Микрофон → VAD → WAV-сегменты → faster-whisper → текст в stdout.

## Возможности

- Захват микрофона через `parec` (PulseAudio/PipeWire).
- Детекция речи silero-vad на чанках по 32 мс.
- Нарезка сегмента по паузе (`--chunk-silence`) с пред-роллом (`--pre-roll`).
- Транскрипция `large-v3-turbo` на CUDA (или CPU).
- Ограничение языка списком `--languages` с фолбеком при мисдетекте.

## Установка

Нужен Python ≥ 3.10. CUDA-библиотеки cuBLAS/cuDNN ставятся из pip-пакетов
`nvidia-*` и не требуют системного CUDA Toolkit.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[cuda]'   # без GPU: .venv/bin/pip install -e .
```

Захват звука идёт через `parec` из пакета `pulseaudio-utils`:

```bash
sudo pacman -S pulseaudio-utils    # Arch
```

## Запуск

```bash
.venv/bin/jarvis-stt                 # large-v3-turbo на CUDA, языки ru/en
.venv/bin/jarvis-stt --device cpu    # без GPU
.venv/bin/jarvis-stt --language ru   # только русский (жёстко)
```

Эквивалент без установки entry point:

```bash
.venv/bin/python -m stt_stream
```

CUDA-каталоги `nvidia/cublas/lib` и `nvidia/cudnn/lib` из venv подставляются в
`LD_LIBRARY_PATH` автоматически (`stt_stream/bootstrap.py`); shell-обёртка не
нужна.

## Модели

Модель Whisper скачивается faster-whisper автоматически при первом запуске.

Для предзагрузки в кэш или скачивания через зеркало есть `download_model.sh`:

```bash
./download_model.sh                                # large-v3-turbo
HF_ENDPOINT=https://hf-mirror.com ./download_model.sh
./download_model.sh --tiny                         # маленькая модель для проверки
```

VAD-модель silero скачивается автоматически в `models/silero_vad.onnx`.

## Параметры

| Флаг | По умолчанию | Назначение |
| :--- | :--- | :--- |
| `--model` | `large-v3-turbo` | Модель faster-whisper |
| `--device` | `cuda` | Без фолбека: при отсутствии CUDA — ошибка с подсказкой |
| `--compute-type` | — | Точность вычислений; по умолчанию `float16` на CUDA |
| `--language` | — | Жёстко заданный язык, напр. `ru` |
| `--languages` | `ru,en` | Допустимые языки; мисдетект → первый из списка |
| `--pre-roll` | `0.5` | Запас аудио перед стартом речи, с |
| `--chunk-silence` | `2.0` | Сколько секунд VAD=false, чтобы завершить запись, с |
| `--threshold` | `0.5` | Порог VAD |
| `--sample-rate` | `16000` | Частота дискретизации |
| `--out-dir` | `segments` | Каталог WAV-чанков |
| `--keep-audio` | — | Не удалять WAV после расшифровки |
| `--input-device` | — | Имя источника PulseAudio |
| `--list-devices` | — | Список источников PulseAudio и выход |

## Документация

Устройство пайплайна и точки расширения — [docs/architecture.md](docs/architecture.md).
