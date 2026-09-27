# Jarvis

Личный цифровой ассистент Georgiy Shonichev. Ядро находится в разработке.

`.env` содержит только `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` и
необязательный `LLM_REASONING_EFFORT` (пусто — дефолт провайдера).
Остальные настройки заданы в коде. Хранилище — `.jarvis` в корне проекта.
STT и TTS настроены на CUDA без CPU fallback; VAD работает на CPU.
Для TTS используется установленный бинарник `s2`.

Запуск: `python -m jarvis`.
Тесты: `python -m unittest discover -s tests`.

[GPL-3.0-only](LICENSE). Copyright (C) 2026 Georgiy Shonichev.
