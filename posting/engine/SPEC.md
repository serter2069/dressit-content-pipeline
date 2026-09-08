# SPEC: декларативное описание пайплайна (proto fshorts#5)

Движок состоит из 4 слоёв:

1. **Раннер** (`runner.py`) — пишется один раз: конечный автомат, исполнение
   стадий, дневные лимиты, сбор ошибок, аудит-лог в posting.db.
2. **Библиотека шагов** (`steps/*.json`) — типизированные шаги с контрактом
   входа/выхода. Сейчас это тонкие обёртки над модулями fshorts
   (subprocess) + нативный шаг `judge` (`judge.py`).
3. **Pipeline spec** (`specs/*.pipeline.json`) — один на ТИП КОНТЕНТА:
   порядок стадий, какой шаг исполняет каждую стадию, где стоят
   judge-гейты, пороги, политика ретраев.
4. **Project config** (`projects/*.config.json`) — один на КАНАЛ: только
   переменные (handle канала, ссылки на токены, слоты расписания,
   промпты/шаблоны, дневные лимиты).

Жёсткое правило: **project config может переопределять только variables,
никогда — шаги или структуру пайплайна.** Раннер падает с ошибкой, если в
project config встречаются ключи `stages` или `steps`.

---

## 1. Pipeline spec (`specs/<name>.pipeline.json`)

```json
{
  "name": "fshorts",                  // имя типа контента (на него ссылается project config)
  "version": "1.0.0",                 // версия; сверяется с pipeline.version проекта
  "description": "...",               // свободный текст
  "states": ["new", "..."],           // документирующий список состояний
  "terminal_states": ["rejected", "published"],
  "stages": [ /* см. ниже */ ]
}
```

### Стадия (элемент `stages[]`)

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| `name` | string | да | Имя стадии (для логов и истории judge-гейтов). |
| `from` | string | да | Исходное состояние job.json (`state`). |
| `to` | string | да | Целевое состояние после успешной стадии. |
| `step` | string | да | Логическое имя шага → `steps/<step>.json`. НЕ путь к файлу. |
| `on_fail` | `"retry"` \| `"hold"` | нет | `retry` (по умолчанию): job остаётся в `from`, следующий тик повторит. `hold`: job уходит в `review` с записанной ошибкой. |
| `daily_limit` | `"generate"` \| `"publish"` | нет | Гейт дневного лимита (значения — из project config). |
| `publish_window` | bool | нет | Стадия выполняется только в окне публикации: с блоком `schedule` — ±30 мин от `scheduled_at` конкретного job (см. ниже), иначе ±30 мин от слота `schedule_slots_utc`. |
| `judge` | object | нет | Judge-гейт ПОСЛЕ шага стадии (см. ниже). |

Порядок `stages` в массиве = порядок пайплайна. На одно состояние `from` —
одна стадия. Состояния, которых нет ни в одном `from`, — терминальные или
ручные.

### Judge-гейт (`stage.judge`)

Гейт — первоклассный объект МЕЖДУ стадиями: выполняется после успешного
шага стадии, до того как job считается прошедшим дальше.

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| `model` | string | да | Модель OpenRouter — просто параметр конфига. Текст: `z-ai/glm-5.3`; мультимодал (картинки/видео): `z-ai/glm-5.3-flash`. |
| `rubric` | string | нет* | Инлайн-рубрика (промпт критериев). |
| `rubric_file` | string | нет* | Путь к файлу рубрики (относительно корня engine или каталога spec). *Обязателен один из `rubric`/`rubric_file`. |
| `artifact` | string | нет | Что судим. `@<поле>` — поле job.json (например `@script`), иначе — файл относительно каталога job. По умолчанию `@script`. |
| `threshold` | number | нет | Порог прохода, 0–10 (по умолчанию 7.0). Итоговый pass = `score >= threshold`; `pass` модели пишется в `model_pass` для аудита. |
| `retries` | int | нет | Повторы при ИНФРАСТРУКТУРНЫХ ошибках (сеть, битый JSON). Провал по содержанию (низкий балл) не ретраится. |
| `mode` | `"stub"` | нет | Гейт-заглушка: авто-пасс, `judge_result.json` пишется с `stub: true`. |

Результат гейта: модель вызывается с `temperature=0` и принудительным
JSON-ответом `{"score": 0-10, "violations": [...], "pass": bool}`; вердикт
пишется в `<job>/judge_result.json` и в историю `job.judges[]`.

**Провал гейта → job уходит в state `review`**, причина видна в
`judge_result.json` / `job.judges[]` — публикация заблокирована до человека.
Инфраструктурная ошибка гейта (после ретраев) → job откатывается в `from`
и повторит стадию на следующем тике (шаги должны быть идемпотентны).

## 2. Библиотека шагов (`steps/<name>.json`)

```json
{
  "name": "scriptgen",
  "type": "subprocess",              // subprocess | stub | manual
  "command": ["python3", "/root/fshorts/scriptgen.py", "--job", "{job_id}"],
  "cwd": "/root/fshorts",
  "env": {"FSHORTS_JOBS_DIR": "{jobs_root}"},
  "timeout": 600,
  "stub": false,                     // true → шаг-заглушка (no-op, см. ниже)
  "note": "почему застаблено / особенности"
}
```

- **subprocess** — запуск команды. Контракт в стиле fshorts: модуль сам
  читает/пишет `<jobs_root>/<job_id>/job.json` и сам переводит `state`;
  если модуль завершился с кодом 0 и НЕ сменил state — раннер сам ставит
  `stage.to`. Плейсхолдеры в command/env/cwd: `{job_id}`, `{job_dir}`,
  `{jobs_root}`, `{engine_root}`. Плюс env раннера: `ENGINE_JOB_ID`,
  `ENGINE_JOB_DIR`, `ENGINE_JOBS_ROOT`, `ENGINE_PROJECT`,
  `ENGINE_VARS` (JSON variables проекта).
- **stub** (или `"stub": true`) — no-op успех, раннер переводит state.
  Так помечены шаги, чья обвязка ещё не готова (см. «Заглушки»).
- **manual** — ручной гейт (например `review → approved` через
  Telegram-бота): раннер никогда не двигает такую стадию сам, state
  переводит внешний актор.

Spec ссылается на шаги по ЛОГИЧЕСКОМУ ИМЕНИ (`"step": "scriptgen"`), а не
по путям — привязка имён к модулям живёт только в `steps/*.json`.

## 3. Project config (`projects/<name>.config.json`)

```json
{
  "name": "demo-fashion",            // уникальное имя канала (= project_alias в runs)
  "pipeline": {"ref": "fshorts", "version": "1.0.0"},
  "channel": "@demo-fashion",
  "jobs_root": "jobs/demo-fashion",  // относительно engine/; дефолт jobs/<name>
  "variables": {                     // ЕДИНСТВЕННОЕ, что может менять проект
    "channel_niche": "...",
    "sponsor_cta": "...",
    "telegram_chat_id_ref": "env:TELEGRAM_CHAT_ID"   // токены — по ссылке, не значением
  },
  "schedule_slots_utc": ["14:00", "18:00", "22:00"],
  "schedule": {                          // fshorts#4: человекоподобное расписание
    "timezone": "America/Los_Angeles",
    "slots": ["11:00", "19:00"],
    "jitter_minutes": 45,
    "days": [0, 1, 2, 3, 4, 5, 6],
    "max_per_day": 2
  },
  "daily_limits": {"generate": 5, "publish": 2}
}
```

- `pipeline.ref` + `version` должны точно совпасть со spec, иначе раннер
  отказывается стартовать.
- `variables` попадают в job.json (`job.vars`) и в env шагов
  (`ENGINE_VARS`). Секреты храним как `env:ИМЯ`-ссылки, не plaintext.
- Ключи `stages`/`steps` в project config ЗАПРЕЩЕНЫ (ошибка загрузки).

### Блок `schedule` (proto fshorts#4)

Человекоподобное расписание публикаций с детерминированным джиттером
(`engine/scheduler.py`):

| Поле | Тип | Смысл |
|---|---|---|
| `timezone` | string | Тайзона канала (IANA, напр. `America/Los_Angeles`). Слоты заданы в ЛОКАЛЬНОМ времени канала. |
| `slots` | string[] | Базовые слоты `"HH:MM"` по локальному времени канала. |
| `jitter_minutes` | int | Разброс ±N минут вокруг слота. |
| `days` | int[] | Дни недели (0 = понедельник). **Пауза канала = `"days": []`.** |
| `max_per_day` | int | Максимум публикаций в день (и слотов в день). |

Семантика джиттера: фактическое время = слот + смещение из
`SHA256(channel|date|slot_index)` в диапазоне `[-jitter, +jitter]`.
Смещение ДЕТЕРМИНИРОВАНО: одинаково для всех, воспроизводимо навсегда,
видно на неделю вперёд (`scheduler.py --plan <config.json>`), но снаружи
выглядит по-человечески (вчера 19:07, сегодня 19:38). Никакого
глобального `random.seed` — только хеш.

Поток публикации: публикуются ТОЛЬКО approved items. При approve раннер
(best-effort, никогда не роняет тик) вызывает `bind_job` → item
привязывается к ближайшему свободному вычисленному слоту →
`items.scheduled_at` пишется в posting.db. Стадия с `publish_window: true`
срабатывает только в окне ±30 минут от `scheduled_at` этого job и с
учётом `max_per_day`. Если блока `schedule` нет — действует легаси-гейт
по статическим `schedule_slots_utc` (как раньше).

Колонка `items.scheduled_at` уже входит в каноническую схему posting.db;
для старых БД `scheduler.py` делает `ALTER TABLE items ADD COLUMN
scheduled_at TEXT` автоматически.

Проекты pre-engine эры (fshorts, dressit-shorts, городские каналы
event-hero) НЕ имеют project config движка: для них хаб выводит
расписание из `channels.publish_times_utc` (тайзона UTC, джиттер
±30 мин по умолчанию). Натуральных JSON-конфигов на город в event-hero
нет (там только state-файлы планировщика), поэтому блок `schedule`
живёт только в engine-era project configs — при миграции города на
движок блок добавляется в его `projects/<name>.config.json`.

## 4. Формат job.json (совместим с fshorts)

`<jobs_root>/<id>/job.json`: `id` (YYYYMMDD-hex4), `state`, `created_at`,
`project`, `pipeline`, `topic`, `vars`, `errors[]` (ошибки дописываются,
не затираются), `judges[]` (история гейтов). State-машина та же, что у
fshorts (`new → scripted → … → published`), поэтому дашборды могут
переиспользовать логику. Движок работает ТОЛЬКО со своим jobs root
(`/root/posting/engine/jobs/...`) и никогда не трогает `/root/fshorts/jobs`.

## 5. Логирование ранов

Каждый `--tick` пишет строку в `runs` posting-хаба
(`sqlite3 /root/posting/data/posting.db`, переопределение — `POSTING_DB`):
`project_alias` = имя проекта, `pipeline` = `engine-tick:<spec>`,
статус running → ok/error, `summary` = JSON `{advanced, transitions}`.
Все обращения к хабу best-effort: падение хаба не ломает тик.

## 6. Заглушки (stubbed) на момент прототипа

- **Frame-judge** (гейт после `rendered`, модель `z-ai/glm-5.3-flash`) —
  только конфиг: извлечение кадров/видео в движок не заведено, гейт стоит
  в `mode: "stub"` и авто-пассит. Рубрика `rubrics/frame.md` — черновик.
- **Шаги-обёртки fshorts** (`scriptgen`, `tts`, `media`, `render`,
  `approve`, `upload`) в манифестах помечены `"stub": true`:
  - `scriptgen.py` жёстко зашил `JOBS_DIR=/root/fshorts/jobs`
    (ROOT = каталог скрипта) и не видит jobs движка;
  - `tts.py` резолвит jobs через `FSHORTS_ROOT`, который одновременно
    используется для поиска конфига/музыки — перенаправление ломает конфиг;
  - `media.py`/`render.py` не проверены на чужом jobs root;
  - `approve_bot.py`/`upload.py` честно читают `FSHORTS_JOBS_DIR` — самые
    портируемые, первые кандидаты на расстабливание (для upload — только
    на полностью подключённом проекте, иначе реальная публикация!).
- Сценарный judge (`z-ai/glm-5.3`, после `new→scripted`) — **боевой**,
  реально ходит в OpenRouter.
