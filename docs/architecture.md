# Архитектура Jarvis

Jarvis состоит из главного агента, произвольного количества субагентов,
встроенных компонентов ядра и пользовательских модулей. Все взаимодействия
проходят через пару event-action. Tool calling не используется.

## Протокол модели

Событие — единица входа в модель. Входное сообщение с ролью `user` содержит
ровно одно JSON-событие:

```json
{"type":"telegram.message","data":{"chat_id":482731905,"text":"Привет"}}
```

Роль `user` здесь является техническим способом передать входные данные в
Chat Completions. Она не утверждает, что событие создано человеком.

Ответ модели всегда имеет одну структуру:

```json
{
  "actions": [
    {"type":"telegram.send","data":{"chat_id":482731905,"text":"Да."}}
  ]
}
```

Схема ответа строится заново из актуального реестра действий каждого агента
и передаётся в `response_format` с `json_schema` и `strict=true`. Для каждого
действия схема связывает `type` с точной схемой его `data`. В ответе можно
вернуть несколько действий. `no_action` разрешён только один и без соседних
действий.

Технические идентификаторы, адресаты и источники не генерируются моделью.
Они хранятся во внутреннем конверте `Event`. В модели остаётся только единый
контракт `type` + `data`. Результат исполнения имеет такое же представление:

```json
{
  "type":"action_result",
  "data":{
    "action_id":"act-8f35a1c2d091",
    "action_type":"telegram.send",
    "status":"success",
    "result":{"message_id":77},
    "error":null
  }
}
```

## Очередь и параллельность

У каждого агента есть собственная история, очередь событий и один цикл
обращения к модели. Один запрос модели выполняется последовательно, чтобы
история не перемешивалась. Все действия одного ответа отправляются в общий
пул исполнения одновременно.

```text
EventBus
  ├─> main agent queue
  ├─> developer agent queue
  └─> любой другой agent queue

agent queue
  └─> пачка событий
        └─> один model request
              └─> несколько parallel actions
                    └─> отдельный action_result на каждое действие
```

Пока модель генерирует ответ или запущенные действия ещё выполняются, новые
события добавляются в очередь. Когда текущий цикл заканчивается, агент
забирает всю накопленную очередь. Каждое событие становится отдельным
сообщением `user`, даже если пачка добавляется одним запросом:

```text
user: {"type":"action_result","data":{...}}
user: {"type":"telegram.message","data":{...}}
user: {"type":"action_result","data":{...}}
```

`no_action` не исполняется, не порождает результат и не очищает очередь. Он
завершает текущий цикл; следующее событие снова запускает модель.

## Встроенные возможности

Компоненты ядра находятся в `jarvis/` и не загружаются из пользовательских
каталогов. Базовые действия главного агента:

- `speech` — поставить текст в TTS и дождаться завершения воспроизведения;
- `agent.spawn` — запустить субагента по пресету или с заданной конфигурацией;
- `agent.message` — передать сообщение родителю или ребёнку;
- `agent.interrupt` — остановить субагента;
- `agent.delete` — остановить и удалить субагента;
- `agent.list` — получить состояния агентов;
- `agent.preset_create`, `agent.preset_delete`, `agent.preset_list` — сохранять
  и использовать типы субагентов;
- `module.create`, `module.update`, `module.delete` — запустить разработчика
  модулей;
- `module.list`, `module.describe` — получить сведения о подключениях;
- `no_action` — завершить цикл ожиданием.

Результаты и сообщения субагентов приходят главному агенту событиями
`agent.task`, `agent.message`, `agent.completed`, `agent.failed`,
`agent.interrupted` и `agent.deleted`.

Пользовательские пресеты сохраняются в `.jarvis/agents/presets.json`, а
встроенный `module_builder` всегда берётся из кода ядра.

## Субагенты

Экземпляр агента имеет собственную историю, очередь и настройки. Его родитель
задаёт пресет, мастер-промпт и список разрешённых действий. Главный агент
может запустить несколько субагентов одновременно. Субагент обращается с
сообщениями только к своему родителю; главный агент может обращаться к любому
своему ребёнку.

Встроенный пресет `module_builder` получает действия:

- `workspace.list`, `workspace.read`, `workspace.write`, `workspace.delete`;
- `process.run`;
- `module.validate`, `module.complete`;
- `agent.message`, `no_action`.

У него нет `agent.spawn`, поэтому он не создаёт субагентов. Его мастер-промпт
жёстко зашит в `jarvis/prompts.py`. Ограничение рабочей папкой задаётся этим
мастер-промптом; отдельная ОС-изоляция не вводится.

## Пользовательские модули

Модули находятся только в:

```text
.jarvis/modules/<module-name>/
  module.json
  module.py
```

`module.json`:

```json
{
  "name":"telegram",
  "version":"1.0.0",
  "description":"Telegram long polling",
  "entrypoint":"module.py",
  "factory":"create_module"
}
```

`module.py` может объявить только действия:

```python
from jarvis.module_api import Module, action


def send(data, ctx):
    return {"sent": True, "text": data["text"]}


def create_module():
    return Module(
        name="telegram",
        version="1.0.0",
        description="Telegram actions",
        actions=(action(
            "telegram.send",
            "Отправить сообщение",
            {
                "type": "object",
                "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["chat_id", "text"],
                "additionalProperties": False,
            },
            send,
        ),),
    )
```

Или только обработчик:

```python
from jarvis.module_api import Module, event, event_handler


def poll(ctx):
    if not ctx.stop_event.wait(5):
        ctx.emit("vacuum.finished", {"robot": "roborock"})


def create_module():
    return Module(
        name="vacuum",
        version="1.0.0",
        description="События пылесоса",
        handlers=(event_handler(
            "vacuum.poller",
            "Следит за состоянием пылесоса",
            (event(
                "vacuum.finished",
                "Уборка завершена",
                {
                    "type": "object",
                    "properties": {"robot": {"type": "string"}},
                    "required": ["robot"],
                    "additionalProperties": False,
                },
            ),),
            poll,
        ),),
    )
```

Обработчик получает `ModuleContext`, публикует события через `ctx.emit()` и
завершается после установки `ctx.stop_event`. Действие получает аргументы и
`ActionContext`; результат должен быть JSON-совместимым. После подключения
модуля его действия сразу появляются в реестре и в строгой схеме следующего
запроса. При следующем запуске Jarvis повторно сканирует эту папку и добавляет
описания модулей в начальный контекст.

При `module.update` действующий экземпляр загружается заново после
успешного `module.complete`. При `module.delete` обработчики останавливаются,
действия удаляются из реестра, а каталог модуля удаляется.

## Поток речи

Текущий STT сохраняет существующую цепочку `parec → SileroVAD → WAV →
faster-whisper`, но после расшифровки публикует событие:

```json
{"type":"speech","data":{"text":"Привет, Джарвис"}}
```

Главный агент выдаёт действие `speech`:

```json
{"actions":[{"type":"speech","data":{"text":"[professional] Да, слушаю."}}]}
```

Действие ставит текст в TTS и ждёт фактического завершения воспроизведения.
До этого main-агент занят, поэтому новые события только накапливаются. Если
приходит новое STT-событие, TTS перебивается, текущая и все ожидающие реплики
получают отдельные `action_result` с `status: "error"` и
`error: "interrupted"`, а новое событие остаётся в очереди агента.
