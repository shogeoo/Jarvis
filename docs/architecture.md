# Архитектура

## Поток данных

```
parec (PulseAudio/PipeWire)
  └─> MicStream            jarvis/audio.py    очередь чанков int16, 32 мс (512 сэмплов @16 кГц)
        └─> SileroVAD      jarvis/vad.py      вероятность речи на каждом чанке (CPU, onnxruntime)
              └─> Segmenter jarvis/vad.py      конечный автомат; при паузе отдаёт готовый WAV
                     └─> queue.Queue                передача сегментов рабочему потоку
                           └─> Transcriber          jarvis/transcribe.py  faster-whisper, CUDA/CPU
                                 ├─> Printer        jarvis/printer.py     строка текста + пустая строка
                                 └─> Assistant      jarvis/assistant.py   реплики -> OpenAI-совместимый API
                                       └─> Printer  jarvis/printer.py     ответ "Jarvis: ..."
```

## Компоненты

- **`audio.MicStream`** — запускает `parec --format=s16le --rate=... --channels=1
  --file-format=raw`, читает фиксированные кадры и складывает их в очередь.
  `--input-device` пробрасывается в `parec --device=<имя>`.
- **`vad.SileroVAD`** — ONNX-инференс silero-vad. Хранит рекуррентное состояние
  (`state`) и сбрасывает его при старте новой речи.
- **`vad.Segmenter`** — конечный автомат:
  `тишина -> речь -> запись WAV (с пред-роллом) -> тишина >= chunk-silence -> готов сегмент`.
  Пред-ролл хранится в `deque(maxlen=pre_roll * sr / frame)`.
- **`transcribe.Transcriber`** — ленивая загрузка модели; при `--language` язык
  задаётся жёстко, иначе автоопределение, и если язык вне `--languages`, сегмент
  перегоняется повторно с первым языком из списка.
- **`bootstrap`** — до импорта CTranslate2 добавляет каталоги
  `nvidia/{cublas,cudnn}/lib` из venv в `LD_LIBRARY_PATH`; при изменении env
  перезапускает процесс.
- **`assistant.Assistant`** — если задана модель (`OPENAI_MODEL`/`--llm-model`),
  держит `system`-промпт и полную историю сообщений. `submit(text)` кладёт
  реплику в очередь; отдельный поток, как освободится, забирает все
  накопившиеся реплики, отправляет их одним запросом `chat/completions` и
  передаёт ответ в `Printer.print_reply`. Ошибки сети печатаются в stderr, а
  реплики остаются в истории и уходят при следующей отправке.

## Инварианты

- Аудио везде моно, int16, 16 кГц; размер кадра `FRAME_SIZE = 512`.
- WAV-сегменты пишутся через `wave` и удаляются после расшифровки, если не
  указан `--keep-audio`.
- Модель и VAD загружаются один раз в рабочем потоке; основной поток только
  читает микрофон и подаёт кадры в VAD.
- На выходе — по строке на сегмент, между расшифровками пустая строка.
- История диалога не обрезается: каждая реплика пользователя и ответ
  ассистента остаются в `messages` до конца сессии.

## Точки расширения

- Другой источник звука — заменить `MicStream` (интерфейс: очередь `frames`).
- Другой детектор речи — заменить `SileroVAD.is_speech`/`reset`.
- Другой ASR — заменить `Transcriber.transcribe_file(path) -> str`.
