# Jarvis — голосовой ассистент

Локальное распознавание речи (русский/английский) с нарезкой на аудио-чанки
по паузам. Микрофон → VAD → WAV-сегменты → faster-whisper → LLM →
озвучка ответа через Fish Audio S2 Pro. Всё локально (LLM и TTS — внешние
эндпоинты, задаются в `.env`).

## Возможности

- Захват микрофона через `parec` (PulseAudio/PipeWire).
- Детекция речи silero-vad на чанках по 32 мс.
- Нарезка сегмента по паузе (`--chunk-silence`) с пред-роллом (`--pre-roll`).
- Транскрипция `large-v3-turbo` на CUDA (или CPU).
- Ограничение языка списком `--languages` с фолбеком при мисдетекте.
- LLM-диалог (OpenAI-совместимый API): мастер-промпт `system_prompt.txt` и
  полная история сообщений, обработка в отдельном потоке.
- Озвучка ответов через Fish Audio S2 Pro (`s2.cpp`): профиль голоса
  `.s2voice`, инлайновые теги, стриминговое воспроизведение.

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

## LLM (OpenAI-совместимый API)

Если задан `LLM_MODEL`, каждая расшифровка уходит в OpenAI-совместимый чат, а
ответ печатается с префиксом `Jarvis:`. Ассистент держит контекст
(мастер-промпт + вся история сообщений, без обрезки) и работает в отдельном
потоке: пока он занят, новые реплики накапливаются и отправляются одним
запросом.

```bash
cp .env.example .env       # LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
$EDITOR system_prompt.txt  # мастер-промпт
.venv/bin/jarvis
```

Пути переопределяются флагами `--env-file` и `--system-prompt`; `--no-llm`
выключает LLM. Без `LLM_MODEL` работает только расшифровка.

## TTS (Fish Audio S2 Pro)

Ответ LLM озвучивается через [s2.cpp](https://github.com/rodrigomatta/s2.cpp)
(`q4_k_m`, CUDA). Jarvis сам поднимает сервер при старте, если он не запущен, и
останавливает его при выходе — модель в VRAM только пока работает Jarvis.
Внешний сервер (запущенный вручную) Jarvis не трогает.

Референсный голос уже в репозитории: `assets/voices/jarvis_reference.mp3` и
`assets/voices/jarvis_reference.txt`. При первом запуске профиль
`assets/voices/jarvis.s2voice` создастся автоматически.

Настройки TTS в `.env` (шаблон — `.env.example`): `TTS_URL`, `TTS_VOICE`,
`TTS_VOICE_DIR`, `TTS_AUTOSTART`, `TTS_SERVER_BIN`, `TTS_MODEL`,
`TTS_TOKENIZER`, `TTS_SERVER_ARGS`, `TTS_REFERENCE`, `TTS_REFERENCE_TEXT`.

Синтез идёт стримом PCM в `ffplay` (нужен пакет `ffmpeg`). Речь управляется
инлайновыми тегами в квадратных скобках (`[whisper]`, `[excited]`, `[sigh]` и
т.п.) — их расставляет LLM согласно мастер-промпту; набор тегов свободный и не
ограничен. `--no-tts` отключает озвучку.

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
| `--env-file` | `.env` | Файл с параметрами LLM и TTS |
| `--system-prompt` | `system_prompt.txt` | Файл мастер-промпта |
| `--no-llm` | — | Только расшифровка, без LLM |
| `--no-tts` | — | Не озвучивать ответы |

## Документация

Устройство пайплайна и точки расширения — [docs/architecture.md](docs/architecture.md).
