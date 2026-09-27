# Jarvis

Jarvis — персональный event-thinking-action ассистент. Он получает поручения,
выбирает actions в строгом JSON, создаёт помощников и разрабатывает дополнительные
возможности через встроенный module_manager. Tool calling не используется.

Copyright (C) 2026 Georgiy Shonichev. [GPL-3.0-only](LICENSE).

## Быстрый старт

Поддерживаемая платформа: Linux, Python 3.10+. Для голоса нужны PulseAudio или
PipeWire с PulseAudio-совместимостью, команды `parec` и `ffplay`.
LLM запускается отдельно либо предоставляется совместимым API-провайдером.
API должен поддерживать streaming и JSON Schema structured responses.

1. Установите зависимости и модели:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install -e .
   jarvis --init-env
   jarvis --download-models
   ```

   На Debian/Ubuntu системные аудио-инструменты: `sudo apt install ffmpeg pulseaudio-utils`.
   На Arch Linux: `sudo pacman -S ffmpeg libpulse`.
   Стандартная установка работает со STT на CPU при настройках из .env.example.
   Для NVIDIA CUDA установите `python -m pip install -e '.[cuda]'` и укажите
   `STT_DEVICE=cuda`, `STT_COMPUTE_TYPE=float16`.

2. Заполните в .env `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`.
   Локальному API без авторизации можно передать непустой фиктивный ключ.
   Пустой или отсутствующий `LLM_REASONING_EFFORT` использует дефолт провайдера:
   параметр не отправляется в API. Явные значения зависят от выбранного провайдера.

3. Запустите:

   ```bash
   jarvis
   # эквивалентно:
   python -m jarvis
   ```

4. Дайте голосовое поручение. Для текстового поручения без микрофона:

   ```bash
   jarvis --message 'Разработай дополнительную возможность для моей задачи'
   ```

Jarvis создаёт недостающую структуру хранилища и базовые presets автоматически.
Ничего копировать из чужого runtime не требуется. Ctrl+C выполняет штатное
завершение. При следующем запуске сохранённые экземпляры и contexts восстанавливаются.

## Fish Audio: единственное внешнее исключение

Для озвучки необходимо **самостоятельно установить бинарник `s2`** из
[s2.cpp](https://github.com/rodrigomatta/s2.cpp) и добавить его в PATH либо
указать абсолютный `TTS_SERVER_BIN`. Он не устанавливается через pip Jarvis.
Используйте инструкции сборки выбранного CPU/CUDA backend в upstream.

После установки s2 повторите `jarvis --download-models`: команда скачает
GGUF и tokenizer из [хранилища весов](https://huggingface.co/rodrigomt/s2-pro-gguf).
Веса имеют собственную Fish Audio Research License; лицензия Jarvis их не заменяет.

Если s2 отсутствует, Jarvis запускается штатно, **action speech исключается из
каталога и схемы модели**. Распознавание речи и speech_detected работают
независимо. Только при отсутствии s2 доступен системный reply для ответа
в терминале. Отключённый или не запустившийся TTS также не рекламируется как
доступное действие; при установленном s2 это не включает reply. Для CUDA-сборки s2 задайте
`TTS_SERVER_ARGS=["--cuda", "0", "-ngl", "-1"]`; для CPU — `[]`.

## Конфигурация и данные

Все параметры базового запуска описаны в [.env.example](.env.example).
`--env-file /path/to/.env` выбирает другой файл. Относительные пути хранилища
привязаны к каталогу этого файла. Сейчас default — `.jarvis`; автоматического
переноса в домашнюю конфигурацию нет. `JARVIS_DIR` уже поддерживает абсолютный
путь, поэтому расположение данных не связано с установленным Python-пакетом.

Модели STT кешируются через faster-whisper/Hugging Face, VAD и профиль голоса —
в runtime выбранного хранилища. TTS_MODEL/TTS_TOKENIZER задают пути весов.
Сначала подготовьте модели, затем запускайте ассистента. Сбой загрузки речи
показывается; текстовое поручение всё равно можно передать через --message.

Contexts, credentials, logs, веса и пользовательские расширения в репозиторий
не входят. Установка wheel включает базовые prompts, SDK-инструкции и голосовые
референсы; не требует запуска из исходного дерева.

## Базовая поставка

- Ядро агентов, дерево экземпляров, FIFO, protocol, dispatch и lifecycle.
- Streaming модели, interrupt, retry и persistent contexts с restore.
- Capability SDK, метаданные без запуска runtime, процессы внешних единиц.
- Presets main (protected singleton) и module_manager.
- Системное управление агентами, presets, automation и назначениями.
- send_message_to_agent для каждого агента.
- reply для текстовых ответов main в терминале, только при отсутствии s2.
- Файловые и bash-инструменты module_manager: read_file, write_file, edit_file,
  execute_command, плюс обзор, описание и глобальная пауза capabilities.
- STT/VAD и опциональная озвучка speech через s2.

Дополнительные интеграции пользователь создаёт сам или поручает их Jarvis.
Конкретный каталог расширений не является частью базовой поставки.
Установленные extensions исполняются с правами пользователя; ядро не добавляет
security sandbox или отдельный loader их .env.

## Разработка

```bash
python -m pip install -e '.[dev]'
ruff check jarvis tests
python -m unittest discover -s tests -p 'test_*.py' -v
python -m build
```

Тесты создают собственные временные данные и не используют живое пользовательское
хранилище. CI проверяет тесты, lint, сборку и импорт ресурсов установленного wheel.

[Архитектура](docs/architecture.md) · [SDK](docs/capability-sdk.md) ·
[Участие в разработке](CONTRIBUTING.md).
