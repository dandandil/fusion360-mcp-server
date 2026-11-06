# Ветка `experimental`: что добавлено поверх upstream

Документ ведём мы; авторские `README.md` и `AGENTS.md` не меняем (там 93 инструмента, в ветке — 111). Обоснования решений и список непроверенного — в `TIMELINE.md`. Исходные проблемы — в `fusion-agent-notes.md`.

## Схема запуска

Код и MCP-сервер — на PC-108-7, Fusion с add-in — на Windows. Claude Code на Windows запускает сервер через ssh с обратным туннелем к add-in:

```powershell
claude mcp add fusion360 --scope user -- ssh -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 9876:localhost:9876 PC-108-7 "cd ~/cad_experiments/fusion360-mcp-server && .venv/bin/fusion360-mcp-server"
```

Add-in копируется в `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360MCP` (`scp -r PC-108-7:cad_experiments/fusion360-mcp-server/addon ...`). После правок `command_handler.py`/`model_graph.py` достаточно скопировать и вызвать `reload_handler`; правки моста, транзакций и журнала требуют перезапуска add-in.

При подключении сервер сравнивает хеш add-in (`ping`) с `addon/` в репозитории; если они разные, каждый ответ начинается с предупреждения — скопировать add-in заново и перезапустить его.

Если запуск падает с `Connection closed`, порт на PC-108-7 может держать повисший туннель прошлой сессии: `ssh PC-108-7 fuser -k 9876/tcp`, затем перезапустить Claude Code.

Флаг `FUSION_MCP_TRANSACTIONS=0` (переменная окружения Fusion) отключает транзакции, а с ними журнал, точки отката и `dry_run`.

## Новые инструменты

| Группа | Инструменты | Что дают |
|---|---|---|
| Транзакции | (все правящие инструменты) | один вызов = один шаг undo `MCP: <инструмент>`; упавший вызов откатывается (`rolled_back: true`); `timeline_changes` — удалённые каскадом и сломанные элементы; `user_actions` — что пользователь сделал в Fusion между вызовами |
| Точки отката | `create_checkpoint`, `list_checkpoints`, `rollback_to_checkpoint` | откат серии вызовов с возвратом маркера; отказ, если модель правили не через MCP |
| Таймлайн | `get_timeline` (фильтры, пагинация), `get_feature_info` (`roll`), `get_sketch_info`, `get_dependents` (кэш), `features_near` | структура модели, входы фич, эскиз целиком (связи, размеры, профили), кто от чего зависит, что находится в области |
| Пересчёт | `set_timeline_marker`, `advance_marker`, `compute_all`, `start_job`, `get_job` (`wait_s`) | пересчёт по шагам; долгие вызовы в фоне; вызов дольше 30 с сам становится задачей (`state: running`, `job_id`) |
| Безопасные правки | `dry_run`, `snapshot_references`, `check_references`, `recreate_feature` | пробная правка с откатом; потерянные грани/рёбра и кандидаты; пересоздание OffsetFaces/Sweep/Extrude/Remove на месте |
| Замысел | `set_design_note`, `get_design_notes` | заметки (сборка, печать, крепёж) в атрибутах модели |
| Обзор | `render_view` (`focus`, `center`/`radius`, `show_only`), `get_scene_info` (`limit`) | кадр на узел или эскиз с размерами; без дампа всей модели |
| Разработка | `reload_handler` | перезагрузка обработчиков add-in без перезапуска Fusion |

Правила работы с существующей моделью сервер отдаёт клиенту при подключении (`src/fusion360_mcp/guide.py`) и промптом `edit-existing-model`.

## Новые модули

- `addon/server/transaction.py` — запуск команды внутри Fusion Command, откат, пробный прогон.
- `addon/server/history.py` — журнал шагов и точки отката.
- `addon/server/model_graph.py` — таймлайн, входы фич, граф зависимостей, проверка ссылок (без `adsk`, тестируется на фейках).
- `addon/server/sketch_info.py` — описание эскиза для `get_sketch_info` (без `adsk`).
- `addon/server/version.py`, `src/fusion360_mcp/addon_check.py` — хеш add-in и проверка версии.
- `src/fusion360_mcp/guide.py` — правила для агента.
- `src/fusion360_mcp/call_log.py` — журнал вызовов на стороне сервера: `~/.local/state/fusion360-mcp/calls-ДАТА.jsonl` и `server.log` (`FUSION_MCP_LOG_DIR`, `off` — выключить). Лог add-in — на Windows, `%USERPROFILE%\fusion360mcp.log`.

## Статус

Всё проверено только на поддельном `adsk` (тесты) и в mock-режиме по протоколу MCP. На живом Fusion не запускалось: перед использованием пройти списки «Не проверено» в `TIMELINE.md`.

## Планы

Новых правок не делаем, пока существующие не проверены на живом Fusion.

1. **Живые тесты** (`docs/LIVE_TESTS.md`). Сначала основа транзакций: выполняется ли Command без диалога и откатывает ли `executeFailed = True` изменения. Затем — списки «Не проверено» в `TIMELINE.md`. Заодно проверить:
   - `setTwoSidesExtent` с `ThroughAll` («body not found» из заметок);
   - можно ли править неактивный документ;
   - два экземпляра Fusion (п. 5).
2. **Тихая подмена входов фичи** (Extrude18 переключился на Body26 без ошибки): сравнивать входы изменённых фич (тела, профили, грани) до и после вызова и возвращать разницу; в `dry_run` — так же.
3. **Sweep через текстовые/UI-команды** как запасной путь к `recreate_feature`.
4. **Ссылки на рёбра после `rollTo`:** общий механизм перечитывания, сейчас только внутри `recreate_feature`.
5. **Параллельная работа: второй экземпляр Fusion.** Запуск из `.bat` с `FUSION_MCP_PORT=9877`, туннель `-R 9877:localhost:9877`, сервер с тем же портом. Проверить: доходит ли переменная до Fusion через лаунчер; что происходит с add-in, если порт занят (не мешать второму экземпляру); стабильность на больших моделях. Журнал истории разделить по документам. Docker и параметр `document` отклонены (обоснование в `TIMELINE.md`).
6. **Сравнение с сохранённой версией** (живой тест, рек. 11): отличать новые поломки от старых, скрытых кэшем (`Sketch41`).
7. **Измерения** (репорт, п. 8): минимальное расстояние между телами/гранями, сечение плоскостью, толщина стенок.
8. **Энкодер** (раздел 5 заметок) — отдельная работа.
