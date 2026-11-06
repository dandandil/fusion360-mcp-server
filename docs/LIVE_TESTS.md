# Живые тесты на Fusion (Windows)

Порядок важен: шаги 2–3 — основа остального; если там провал, дальше не идём, а разбираемся. Результаты — в `TIMELINE.md`.

## 0. Подготовка Windows

1. VPN до 10.228.30.5.
2. ssh-ключ (PC-108-7 принимает пароль, ключ добавляется один раз):
   ```powershell
   ssh-keygen -t ed25519
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh daniil@10.228.30.5 "cat >> ~/.ssh/authorized_keys"
   ```
   В `%USERPROFILE%\.ssh\config`:
   ```
   Host PC-108-7
       HostName 10.228.30.5
       User daniil
   ```
   Проверка: `ssh -o BatchMode=yes PC-108-7 echo ok`.
3. Add-in:
   ```powershell
   scp -r PC-108-7:cad_experiments/fusion360-mcp-server/addon "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360MCP"
   ```
   Если папка `Fusion360MCP` уже есть — сначала удалить, иначе scp вложит `addon` внутрь. В Fusion: Utilities → Add-Ins → вкладка Add-Ins → Fusion360MCP → Run (и «Run on Startup»).
4. MCP в Claude Code — команда из `docs/EXPERIMENTAL.md`, затем `claude mcp list`.
5. Правила проекта для сессии на Windows: `ssh PC-108-7 cat cad_experiments/fusion360-mcp-server/CLAUDE.md`.
6. Тестовая модель — **копия**, не исходный `WING_7.5_`. Для шагов 1–4 хватит нового документа с одним телом.

## 1. Связь

- `ping`, `get_scene_info` — ответ из Fusion, не mock; `ping` возвращает `addon_hash`, предупреждения о версии нет.
- Нарочно старый add-in (или правка в копии): каждый ответ начинается с предупреждения о версии.

## 2. Транзакции (новый документ)

- `create_sketch` + `extrude`: появился один шаг undo `MCP: extrude` (Edit → Undo в UI), диалога команды не было.
- Вызов, который упадёт (например, `fillet` с несуществующим ребром): `rolled_back: true`, модель не изменилась, модального окна нет.
- `undo` отменяет весь вызов целиком.
- `execute_code` с созданием фичи — тоже один шаг.
- Вызов во время открытой пользователем команды (например, диалог Extrude в UI): что происходит.

- Долгий вызов (`execute_code` с циклом на 40 с): ответ `state: running` + `job_id`, `get_job` возвращает итог, правка применена один раз.
- Удаление фичи с зависимыми через `execute_code` (копия WING: `Extrude18`): в ответе `timeline_changes.removed_items` совпадает с тем, что пропало в UI.
- Правка в UI между вызовами: следующий ответ содержит `user_actions`.
- После сессии на PC-108-7 есть `~/.local/state/fusion360-mcp/calls-*.jsonl`.

## 3. Откат и журнал

- `create_checkpoint` → `set_timeline_marker` назад → правка → `advance_marker` → `rollback_to_checkpoint`: откат не отказывает, правка снята, маркер вернулся (`timeline_matches_checkpoint: true`). Если нет — движение маркера попадает в undo, переделать.

- `create_checkpoint a` → 3 правки → `rollback_to_checkpoint a`: `timeline_matches_checkpoint: true`.
- Правка руками в UI после точки → `rollback_to_checkpoint` отказывает с `HISTORY_CONFLICT`.
- Ctrl+Z в UI: снялась ли запись в `list_checkpoints.journal`. В логе add-in — id чужих команд: шумные (камера, выбор, измерения) внести в `IGNORED_COMMAND_IDS`.

## 4. Пробный прогон и маркер

- `dry_run` правки параметра: отчёт есть, модель не изменилась, в журнале пусто.
- `set_timeline_marker` назад/вперёд; `start_job compute_all` + `get_job`.

## 5. Чтение на большой модели (копия WING)

- `get_dependents Sketch23 transitive`: `elapsed_s` первого вызова и второго (`graph_cached: true`, должно быть мгновенно).
- `get_feature_info Fillet21` → есть `unreadable`; с `roll=true` они прочитаны, маркер на месте.
- `get_sketch_info Sketch23`, `Sketch25`: связи, размеры, профили совпадают с UI; эскизы в подкомпоненте находятся.
- `execute_code` с `print(1)`: длительность в журнале вызовов (было ~5 с).
- `start_job advance_marker` из позиции 120: время прохода против одного прыжка маркера; `get_job wait_s=60` отвечает, пока идёт пересчёт.

- `get_timeline problems_only`, `features_near` у моторамы, `get_feature_info Extrude18`, `get_dependents Sketch23 transitive`: правдоподобно ли и сколько секунд (таймаут 30 с).
- Входы совпадают с UI (группы в таймлайне, грани по `entityToken`).
- `snapshot_references` → правка → `check_references`.

## 6. Рендер

- `render_view focus=Body73 show_only=[Body73]` (остальные тела вернулись видимыми, в undo нет лишних шагов), `focus=Sketch23`: кадр на узел, у эскиза видны размеры, `camera.view_extents` ≈ `requested_extents`, камера пользователя восстановлена.

## 7. Пересоздание фич (копия WING)

- `recreate_feature OffsetFaces5` с новыми точками граней; затем `SweepFeature` и `ExtrudeFeature` (`Extrude18`, двусторонняя) с новым профилем, `RemoveFeature` с новым телом: то же место в таймлайне, то же имя, зависимые фичи здоровы; провал откатывается целиком; если удаление оригинала тянет за собой зависимые — отказ со списком.

## 8. Заодно (для планов)

- `setTwoSidesExtent` + `ThroughAll` через `execute_code` — воспроизводится ли «body not found».
- Можно ли через `execute_code` создать фичу в неактивном документе.
- Второй экземпляр Fusion с `FUSION_MCP_PORT=9877`: поднялся ли add-in, что пишет первый/второй при занятом порте.
