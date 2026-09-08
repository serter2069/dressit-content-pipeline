# approval — единый approval-flow для всех видео-пайплайнов

Обобщение fshorts-решения (`/root/fshorts/approval_server.py`) на все проекты
постинг-хаба. Proto task fshorts#3.

Бот @Minimesergeibot занят production-вебхуком dressit, поэтому inline-кнопки
(callback_query) невозможны. Решение: в подписи к видео в Telegram лежат три
подписанные HMAC-ссылки; оператор тапает ссылку, этот сервер переворачивает
состояние джобы через адаптер проекта.

## Роуты

```
GET /a/<project>/<job_id>/<sig>   ✅ approve
GET /r/<project>/<job_id>/<sig>   ❌ reject
GET /g/<project>/<job_id>/<sig>   🔁 regenerate (state=new)
GET /healthz                      liveness probe → 200 ok

# LEGACY (старые ссылки из подписей fshorts продолжают работать):
GET /a|r|g/<job_id>/<sig>         project = dressit-fshorts, старая подпись
```

Подписи (первые 16 hex-символов HMAC-SHA256 по `APPROVAL_SECRET`):

- новые ссылки: `HMAC(secret, "<project>:<job_id>")`
- legacy-ссылки: `HMAC(secret, "<job_id>")`

Проект — это alias из таблицы `projects` в `/root/posting/data/posting.db`
(`dressit-fshorts`, `dressit-sf`, `dressit-la`, `dressit-nyc`,
`dressit-miami`, `chesstourism`, `chess-events`, …).

## Адаптеры (`adapters.py`)

Интерфейс адаптера:

```python
class MyAdapter(BaseAdapter):
    def apply(self, job_id: str, action: str) -> tuple[int, str]:
        # action: "a" | "r" | "g"  → состояния approved | rejected | new
        # return (http_code, human_message)
```

Подключение нового пайплайна:

```python
# в adapters.py (или из кода пайплайна до старта сервера)
register("my-project", MyAdapter())
```

Зарегистрировано:

| Проект | Адаптер | Что делает |
|---|---|---|
| `dressit-fshorts` | `FShortsAdapter` | job.json в `/root/fshorts/jobs/<id>/`: approve/reject → state; regen → state=new + чистит script/assets/render + сбрасывает telegram.message_id. Логика зеркалит оригинальный fshorts approval_server. |
| любой другой alias из `projects` | `HubItemAdapter` (fallback) | переворачивает `items.state` в posting.db. Пайплайн проекта сам следит за состоянием хаба (publish approved, перерендер new). |

У каждого решения — аудит в хабе:
- `items.state` → approved/rejected/new (если айтем находится по id / source_ref / суффиксу `-<job_id>`, с предпочтением канала проекта);
- строка в `runs` (`pipeline='approval'`, status ok/error, summary).

Схема posting.db не менялась — используются существующие таблицы.

## Переменные окружения

Читаются из реального окружения, затем `/root/.video-env`,
`/root/fshorts/.env`, `/root/posting/approval/.env` (первое значение побеждает;
`export`-префикс поддерживается).

| Var | Назначение |
|---|---|
| `APPROVAL_SECRET` | обязателен для сервера; общий секрет подписи |
| `APPROVAL_PORT` | порт, default 8477 |
| `APPROVAL_BASE_URL` | публичный base URL для ссылок (default `http://95.217.84.161:8477`, в fshorts/.env уже задан) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | для notify.py |
| `POSTING_DB` | путь к хабу (default `/root/posting/data/posting.db`) |
| `FSHORTS_JOBS_DIR` | override jobs-директории fshorts |

## Как пайплайну отправить видео на ревью (`notify.py`)

```python
import sys; sys.path.insert(0, "/root/posting/approval")
from notify import send_for_review

message_id = send_for_review(
    project="dressit-fshorts",      # alias из posting.db
    job_id=job["id"],
    video_path="/path/to/final.mp4",
    caption_text=f"{hook}\n\n{cta}",  # сценарий/описание
)
```

CLI: `python3 notify.py <project> <job_id> <video.mp4> [текст подписи...]`

Подпись обрезается до 1024 символов (лимит Telegram), содержит текст,
метку `[project] Job: <id>` и три подписанные ссылки нового формата.

fshorts пока продолжает использовать свой `approve_bot.py`, который генерит
legacy-ссылки — сервер их обслуживает (роуты `/a/<job>/<sig>` →
`dressit-fshorts`). Миграция fshorts на `notify.py` — отдельная задача,
ломать ничего не нужно.

## Деплой

systemd unit `approval.service` (уже установлен в
`/etc/systemd/system/approval.service` и enabled):

```bash
systemctl daemon-reload
systemctl enable --now approval.service
systemctl status approval.service
ss -tlnp | grep 8477
journalctl -u approval.service -f
```

Без systemd: `nohup python3 /root/posting/approval/approval_server.py --serve >> /var/log/approval.log 2>&1 &`

## Тесты

```bash
python3 approval_server.py --selftest   # офлайн: temp jobs + temp hub DB,
                                        # legacy+new подписи, 403/404, аудит
python3 notify.py --selftest            # офлайн: mocked Telegram API, caption+links
```

Файлы:

- `approval_server.py` — HTTP-сервис + роуты + подписи + selftest
- `adapters.py` — реестр адаптеров, FShortsAdapter, HubItemAdapter, hub-аудит
- `notify.py` — отправка видео на ревью в Telegram
- `approval.service` — systemd unit
- `README.md` — этот файл
