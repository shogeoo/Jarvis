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

Параметры API задаются в `.env` (шаблон — `.env.example`), мастер-промпт —
в `system_prompt.txt`. Если задан `OPENAI_MODEL`, каждая расшифровка уходит в
OpenAI-совместимый чат, а ответ печатается с префиксом `Jarvis:`. Ассистент
держит контекст (мастер-промпт + вся история сообщений, без обрезки) и
работает в отдельном потоке: пока он занят, новые реплики накапливаются и
отправляются одним запросом.

```bash
cp .env.example .env        # заполни OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
$EDITOR system_prompt.txt   # мастер-промпт
.venv/bin/jarvis
```

Пути переопределяются флагами `--env-file` и `--system-prompt`; `--no-llm`
выключает нейросеть. Без `OPENAI_MODEL` в `.env` работает только расшифровка.

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
| `--env-file` | `.env` | Файл с параметрами API |
| `--system-prompt` | `system_prompt.txt` | Файл мастер-промпта |
| `--no-llm` | — | Только расшифровка, без нейросети |

## Документация

Устройство пайплайна и точки расширения — [docs/architecture.md](docs/architecture.md).
