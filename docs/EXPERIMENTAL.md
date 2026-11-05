# Ветка `experimental`: что добавлено поверх upstream

Документ ведём мы; авторские `README.md` и `AGENTS.md` не меняем (там 93 инструмента, в ветке — 111). Обоснования решений и список непроверенного — в `TIMELINE.md`. Исходные проблемы — в `fusion-agent-notes.md`.

## Схема запуска

Код и MCP-сервер — на PC-108-7, Fusion с add-in — на Windows. Claude Code на Windows запускает сервер через ssh с обратным туннелем к add-in:

```powershell
claude mcp add fusion360 --scope user -- ssh -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 9876:localhost:9876 PC-108-7 "cd ~/cad_experiments/fusion360-mcp-server && .venv/bin/fusion360-mcp-server"
```

Add-in копируется в `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360MCP` (`scp -r PC-108-7:cad_experiments/fusion360-mcp-server/addon ...`). После правок `command_handler.py`/`model_graph.py` достаточно скопировать и вызвать `reload_handler`; правки моста, транзакций и журнала требуют перезапуска add-in.

Флаг `FUSION_MCP_TRANSACTIONS=0` (переменная окружения Fusion) отключает транзакции, а с ними журнал, точки отката и `dry_run`.

## Новые инструменты

| Группа | Инструменты | Что дают |
|---|---|---|
| Транзакции | (все правящие инструменты) | один вызов = один шаг undo `MCP: <инструмент>`; упавший вызов откатывается (`rolled_back: true`) |
| Точки отката | `create_checkpoint`, `list_checkpoints`, `rollback_to_checkpoint` | откат серии вызовов; отказ, если модель правили не через MCP |
| Таймлайн | `get_timeline` (фильтры, пагинация), `get_feature_info`, `get_dependents`, `features_near` | структура модели, входы фич, кто от чего зависит, что находится в области |
| Пересчёт | `set_timeline_marker`, `compute_all`, `start_job`, `get_job` | пересчёт по шагам; долгие вызовы в фоне без таймаута |
| Безопасные правки | `dry_run`, `snapshot_references`, `check_references`, `recreate_feature` | пробная правка с откатом; потерянные грани/рёбра и кандидаты; пересоздание OffsetFaces/Sweep на месте |
| Замысел | `set_design_note`, `get_design_notes` | заметки (сборка, печать, крепёж) в атрибутах модели |
| Обзор | `render_view` (`focus`, `center`/`radius`), `get_scene_info` (`limit`) | кадр на узел или эскиз с размерами; без дампа всей модели |
| Разработка | `reload_handler` | перезагрузка обработчиков add-in без перезапуска Fusion |

Правила работы с существующей моделью сервер отдаёт клиенту при подключении (`src/fusion360_mcp/guide.py`) и промптом `edit-existing-model`.

## Новые модули

- `addon/server/transaction.py` — запуск команды внутри Fusion Command, откат, пробный прогон.
- `addon/server/history.py` — журнал шагов и точки отката.
- `addon/server/model_graph.py` — таймлайн, входы фич, граф зависимостей, проверка ссылок (без `adsk`, тестируется на фейках).
- `src/fusion360_mcp/guide.py` — правила для агента.

## Статус

Всё проверено только на поддельном `adsk` (тесты) и в mock-режиме по протоколу MCP. На живом Fusion не запускалось: перед использованием пройти списки «Не проверено» в `TIMELINE.md`.
