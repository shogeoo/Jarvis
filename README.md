# Jarvis — голосовой ассистент

Локальное распознавание речи (русский/английский) с нарезкой на аудио-чанки
по паузам. Микрофон → VAD → WAV-сегменты → faster-whisper → текст в stdout.
Опционально расшифровка уходит в OpenAI-совместимую нейросеть с контекстом
диалога.

## Возможности

- Захват микрофона через `parec` (PulseAudio/PipeWire).
- Детекция речи silero-vad на чанках по 32 мс.
- Нарезка сегмента по паузе (`--chunk-silence`) с пред-роллом (`--pre-roll`).
- Транскрипция `large-v3-turbo` на CUDA (или CPU).
- Ограничение языка списком `--languages` с фолбеком при мисдетекте.
- Опциональный диалог с OpenAI-совместимым API: system-промпт и полная
  история сообщений, обработка в отдельном потоке.

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
.venv/bin/jarvis                 # large-v3-turbo на CUDA, языки ru/en
.venv/bin/jarvis --device cpu    # без GPU
.venv/bin/jarvis --language ru   # только русский (жёстко)
```

Эквивалент без установки entry point:

```bash
.venv/bin/python -m jarvis
```

CUDA-каталоги `nvidia/cublas/lib` и `nvidia/cudnn/lib` из venv подставляются в
`LD_LIBRARY_PATH` автоматически (`jarvis/bootstrap.py`); shell-обёртка не
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

## Нейросеть (OpenAI-совместимый API)

Если задана переменная `OPENAI_MODEL`, каждая расшифровка уходит в
OpenAI-совместимый чат, а ответ печатается с префиксом `Jarvis:`. Ассистент
держит контекст (system-промпт + вся история сообщений, без обрезки) и
работает в отдельном потоке: пока он занят, новые реплики накапливаются и
отправляются одним запросом.

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.openai.com/v1        # или свой эндпоинт
export OPENAI_MODEL=gpt-4o-mini
export JARVIS_SYSTEM="Ты — Jarvis, отвечай кратко."     # необязательно
.venv/bin/jarvis
```

Флаги `--llm-model`, `--llm-base-url`, `--llm-api-key`, `--llm-system`
переопределяют переменные; `--no-llm` выключает нейросеть. Без
`OPENAI_MODEL`/`--llm-model` работает только расшифровка.

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
| `--llm-model` | env `OPENAI_MODEL` | Модель API; без неё нейросеть выключена |
| `--llm-base-url` | env `OPENAI_BASE_URL` | Эндпоинт OpenAI-совместимого API |
| `--llm-api-key` | env `OPENAI_API_KEY` | API-ключ |
| `--llm-system` | env `JARVIS_SYSTEM` | System-промпт ассистента |
| `--no-llm` | — | Только расшифровка, без нейросети |

## Документация

Устройство пайплайна и точки расширения — [docs/architecture.md](docs/architecture.md).
