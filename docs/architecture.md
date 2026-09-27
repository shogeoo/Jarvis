# Архитектура Jarvis

## Слои

```text
jarvis/
  application.py          сборка компонентов
  cli.py, bootstrap.py    запуск и подготовка моделей
  core/                   agents, protocol, queues, system actions
  infrastructure/         config, memory, automation, diagnostics
  capabilities/           SDK, metadata loader, worker и RPC
  presets/                шаблоны создания экземпляров
  speech/                 STT, VAD и опциональный TTS
  assets/                 общие prompts и базовые личности
tests/                    самостоятельные временные fixtures
docs/                     общий контракт архитектуры и SDK
```

Python-пакет содержит всю базовую поставку. Пользовательское хранилище создаётся
по JARVIS_DIR и исключено из version control. Default пока остаётся .jarvis.
Будущая смена default не требует переноса кода или пакетных ресурсов.

## Протокол и контекст

Model output:
`{"actions":[{"action_id":"name","call_id":"act-1","data":{}}]}`.

action_id определяет вид действия на весь repository; call_id — конкретный
вызов, уникальный во всей жизни контекста одного агента, включая structure errors.
Успешный результат:
`{"type":"call_result","call_id":"act-1","data":{}}`.

Handler event: `{"handler_id":"name","data":{}}`.
Core events используют type/data: system_started, user_message,
message_from_agent, speech_detected, structure_error, capability_error.
Capability error содержит kind/id/error/call_id и адресуется caller.

Каждый input становится отдельным user message. Роль user — транспорт.
Полный JSON модели проверяется перед dispatch. Structure error остаётся в
истории и вызывает новый запрос без искусственного небольшого лимита попыток.
Call IDs ошибочных ответов остаются занятыми. No_action — единственное действие
такого ответа и не запускает execution.

System prompt состоит из person_prompt → master_prompt → model_info → capabilities.
Личность берётся из состояния экземпляра, общие правила и SDK — из пакетных
ресурсов. Model_info описывает modalities. Каталог динамический и содержит
storage_root, system actions, известные внешние units и их схемы.

## Агенты и presets

Preset задаёт личность, protected и стартовые назначения.
Экземпляр владеет собственной личностью, контекстом, очередью и назначениями.
Edit preset не меняет уже живые или восстановленные экземпляры.
Protected preset — singleton; его экземпляр нельзя удалить или изменить извне.

Main управляет агентами, presets, automation и собственными назначениями.
Module_manager имеет системные инструменты разработки и сведения о capabilities.
Любой агент имеет send_message_to_agent. Дополнительные системные инструменты
можно явно назначить в preset, но они не требуют внешних capability hosts.

Interrupt инвалидирует generation и закрывает её stream. Очередь и запущенные
actions сохраняются; поздние chunks не принимаются. Закрытие соединения не
гарантирует прекращение вычисления на сервере любого провайдера.

Delete допускается во время работы. Он снимает регистрации, останавливает
generation, отменяет executions, рекурсивно удаляет descendants и память.
Поздний persist удалённого экземпляра не выполняется.
Remove preset удаляет его экземпляры и descendants, затем шаблон; protected
семантика проверяется до начала удаления.

## Actions, процессы и состояния

Системные actions работают в ядре. Внешняя capability — module, отдельный
action или handler; часть module не включается независимо.
Каждый внешний action invocation получает собственный execution process.
Handlers работают независимо; один длительный вызов не блокирует capability
runtime или другие executions. Таймаута результата нет.

Capability может быть assigned/known, local enabled/disabled, global
running/paused и runtime loaded/unloaded. Эти состояния не смешиваются.
Local disabled остаётся в каталоге и возвращает status=disabled/info.
Global paused действует на всех назначенных агентов и возвращает paused/info.
Running action при disable/pause отменяется; поздний исходный результат не
доставляется. Exactly-once tracker допускает максимум одну финализацию.

Фатальная смерть host очищает процессы и registrations, невозможные результаты
заменяются caller-directed capability errors. Автоматического restart нет.
Системные bash-команды принадлежат ProcessManager, включая дочерние процессы;
удаление агента отменяет соответствующую команду.

## Хранилище и restore

Общая структура:
```text
actions/<id>/action.py
handlers/<id>/handler.py
modules/<id>/module.py
presets/<id>/personprompt.txt, preset.json, capabilities.json, disabled_capabilities.json
capability_state.json
automations.json
memory/<preset>/[<agent_id>/]current/, last/
runtime/logs/, models/, voices/, segments/
```

Capabilities.json содержит modules/actions/handlers. Global pause сохраняется
отдельно. Недостающие структурные файлы создаются; существующие originals не
перезаписываются шаблонами. Повреждённые файлы показываются и сохраняются.

Memory snapshot публикуется после записи metadata и context в staging. Current
становится last, завершённый staging — current. Ошибка persistence логируется,
runtime продолжает работу. Сохраняются только current/last, system message
пересобирается. Restore поднимает старые экземпляры с histories; ошибки не
удаляют память, не заменяют её preset и не создают fresh replacement.

InputPart.name сохраняется через serialization, worker/core и restore.
Имена attachments не перезаписываются: collision получает суффикс (1), (2)…
Исторические ссылки продолжают указывать на прежнее содержимое.

## Automation

Правило имеет automation_id, ровно один event либо call_result trigger и actions
без call_id. Совпадение структурное и полное; у call_result игнорируется только
конкретное значение call_id. Runtime создаёт уникальные auto_act-N, добавляет
обычный assistant message и dispatch-ит действия стандартным путём.
Результаты поступают в тот же context. Partial matching и классификация отсутствуют.
Единый AutomationStore сериализует create/edit/remove, сохраняя соседние правила.

## Речь и запуск

CLI загружает .env до импорта speech settings и подготовки CUDA.
--download-models подготавливает веса без запуска микрофона или model API.
Обычный startup создаёт структуру, поднимает доступную речь и восстанавливает
агентов. После инициализации main получает system_started с локальным ISO datetime.
--message доставляет адресный user_message через обычный EventBus.

STT и TTS независимы. При отсутствии внешнего s2, отключённом или неработающем
TTS action speech исключён из каталога/response schema. Speech_detected остаётся
доступным main. Ctrl+C запускает очистку процессов и сервисов.
Системный reply доступен только при отсутствии настроенного бинарника s2
и выводит текстовый ответ пользователю в терминал через logger.
Отключение или ошибка TTS при установленном s2 не включает reply.
