# posting/engine — декларативный движок пайплайнов (прототип, proto fshorts#5)

Новый видео-проект = КОНФИГ, а не код. Один раннер (`runner.py`) исполняет
пайплайны, описанные JSON-спеками. Формат — в [SPEC.md](SPEC.md).

```
engine/
├── runner.py                    # движок: --tick / --new / --stats / --selftest
├── judge.py                     # нативный LLM-judge шаг (OpenRouter, temperature 0)
├── SPEC.md                      # формат pipeline spec + project config (читать первым)
├── steps/*.json                 # библиотека шагов: логическое имя → модуль/тип
├── specs/fshorts.pipeline.json  # тип контента «fshorts» как данные
├── projects/demo-fashion.*      # пример конфига канала + тема для демо
├── rubrics/*.md                 # рубрики judge-гейтов
└── jobs/<project>/<id>/         # jobs движка (НЕ /root/fshorts/jobs!)
```

## Как создать новый канал (новый project config)

1. Скопируй `projects/demo-fashion.config.json` под новым именем.
2. Поменяй `name`, `channel`, `variables`, слоты и лимиты.
3. Укажи `pipeline.ref`/`version` на существующий spec.
4. Готово: `python3 runner.py --project <name> --tick`.

Менять можно ТОЛЬКО variables — структуру пайплайна проект переопределять
не может (раннер отклонит конфиг с ключами `stages`/`steps`).

## Как создать новый тип контента (новый pipeline spec)

1. Напиши `specs/<type>.pipeline.json`: стадии `from→to`, шаги по именам,
   judge-гейты с моделью/рубрикой/порогом (см. SPEC.md).
2. Переиспользуй существующие шаги из `steps/` или добавь манифест-обёртку.
3. Создай project config со ссылкой на новый spec.

## Когда реально нужен новый код

Только когда появляется новый ТИП шага (не покрывается subprocess-вызовом
существующего модуля, stub или judge-гейтом) — тогда пишется нативный шаг
по образцу `judge.py` и регистрируется в `runner.py` / `steps/`.
Новые каналы и новые пайплайны из существующих шагов кода не требуют.

## Как логируются раны

Каждый тик пишет строку в таблицу `runs` хаба
(`sqlite3 /root/posting/data/posting.db`, переменная `POSTING_DB`
переопределяет): кто запускал (`project_alias`), что (`pipeline` =
`engine-tick:<spec>`), статус, summary с переходами, хвост лога.
Падение хаба не влияет на тик (best-effort, как в fshorts/pipeline.py).

## Быстрый старт

```bash
cd /root/posting/engine
python3 runner.py --selftest                                  # офлайн-проверка
python3 runner.py --project demo-fashion --new projects/demo-fashion.topic.json
python3 runner.py --project demo-fashion --tick
python3 runner.py --project demo-fashion --stats
```

Ограничения прототипа (что застаблено и почему) — SPEC.md, раздел
«Заглушки». Движок никогда не трогает `/root/fshorts/jobs` — продакшн-
пайплайн fshorts работает параллельно и независимо.
