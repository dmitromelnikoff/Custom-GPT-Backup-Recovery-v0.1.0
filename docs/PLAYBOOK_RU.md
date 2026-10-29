# Операционный процесс с нуля — Windows PowerShell

## 0. Предусловия и ограничения

- Python 3.10+, Google Chrome, `pip install -r requirements.txt` (Playwright + psutil).
- В аккаунте должны быть **собственные** Custom GPT, к редакторам которых у вас есть доступ.
- Система восстановления не является официальным экспортом ChatGPT; UI и веб-эндпоинты меняются.
- Диск с резервной копией имеет достаточно места; используйте отдельную приватную папку проекта.
- Чувствительные данные хранятся **локально**, GitHub должен содержать **только код**.
- Никогда не открывайте CDP-порт на внешнем сетевом интерфейсе; оставляйте `127.0.0.1`.

## 1. Развернуть чистый проект

```powershell
mkdir custom-gpt-backup-recovery
cd custom-gpt-backup-recovery
# Извлеките сюда файлы релизного архива GitHub проекта
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 2. Получить snapshot собственных GPT

**Обязательный вход для основного загрузчика:** `Custom_GPT_backup/gpt_list_snapshot.json`.

Объект содержит `gpts[*].resource.gizmo.id`, `display.name` и для каждого Knowledge-файла: `resource.files[*].file_id`, `name`, `size`; сохраните **исходный порядок файлов**. Для общей дедупликации полезны `type`, `created_at`, `last_modified`. Подробности — [DATA_MODEL_RU.md](DATA_MODEL_RU.md).

**Исторический способ, может перестать работать:**

```powershell
python experimental/bootstrap_snapshot_legacy.py --normal-chrome --no-files
```

Скрипт запускает **отдельный Chrome-профиль**, просит вас вручную войти и использует недокументированные *read-only* веб-запросы для снимка и частичного извлечения Instructions/Actions. Никаких гарантий поддерживаемости API. `--no-files` исключает старую ненадёжную прямую попытку скачать Knowledge через backend. На выходе ищите `Custom_GPT_backup/gpt_list_snapshot.json`. Убедитесь, что в нём действительно перечислены ваши GPT и файлы, иначе **не запускайте массовое скачивание**. Создание snapshot не означает автоматического сохранения всех деталей Actions.

Если bootstrap не работает, используйте локально экспортированный/собранный вами реестр **того же формата**. Не подставляйте чужие GPT из истории бесед как «свои».

**Полностью офлайн-альтернатива**, если у вас уже есть таблица имён, размеров и ID файлов, собранная разрешённым вам способом:

```powershell
python tools/snapshot_from_csv.py --csv "C:\PRIVATE\my_gpt_inventory.csv" --out Custom_GPT_backup/gpt_list_snapshot.json
```

Формат столбцов и искусственный пример: `examples/sample_inventory.csv`. Этот конвертер **не собирает данные из ChatGPT**, не восстанавливает отсутствующие `file_id`, не перезаписывает существующий snapshot и не доказывает достоверность исходной таблицы. Для 250 GPT потребуется **полный** реестр файлов.

Профиль `.chatgpt_backup_normal_chrome_profile` содержит авторизацию, его нужно защищать и **никогда не коммитить**.

## 3. Проверить очередь без сети

```powershell
python scripts/download_direct_gpt_knowledge_ui_v11.py --all-gpts --plan-only
```

Проверяем, что GPT ID уникальны, количество Knowledge правдоподобно, а существующие файлы правильно пропущены.

## 4. Пробное скачивание из редактора

Chrome из шага 2 должен оставаться **открытым и авторизованным**, иначе запустите его в том же выделенном профиле с локальным CDP. `--attach` не запускает новую авторизацию.

```powershell
python scripts/download_direct_gpt_knowledge_ui_v11.py --all-gpts --attach --confirm-access-restored --max-gpts 1 --max-attempts 1 --max-files 1 --pause-min 15 --pause-max 30
```

Реальная последовательность: редактор GPT → переключатель «Конфигурация» (может быть `radio`) → раздел «Знания» → кнопка **точно названного** файла → просмотрщик/окно → «Скачать». Скрипт не должен нажимать «Удалить файл» или «Обновить».

Если появляется `CONFIG_KNOWLEDGE_VISIBLE`, но не скачивается, изучите `direct_gpt_editor_ui_probe.json` и `Direct_GPT_UI_probes/`. Не пытайтесь наугад нажимать неизвестные элементы.

## 5. Основной длительный прогон

```powershell
python -u scripts/download_direct_gpt_knowledge_ui_v11.py --unattended --attach --confirm-access-restored --pause-min 15 --pause-max 30
```

Оставьте Chrome и окно PowerShell открытыми. Запуск **синхронный**, не является облачной фоновой задачей. В unattended на Windows скрипт временно препятствует переходу ПК в сон, пока выполняется.

Скрипт безопасно обходит UI-ошибки **отдельных файлов** и откладывает их для второго этапа. Он должен остановиться при сетевых/защитных ограничениях (401, 403, 429), потере соединения или слишком большом числе неудачных GPT. При остановке **не обходите ограничение и не повторяйте немедленно**: проверьте, восстановился ли обычный доступ.

Прогресс сохраняется после файлов, повторный запуск использует уже полученные размеры. **Важно:** размер используется лишь как сигнал наличия; если содержимое ранее взято из Library/архива, происхождение остаётся неподтверждённым.

Полезные флаги для безопасного тестирования: `--skip-gpt-name-regex`, `--skip-gpt-ids`, `--max-gpts`, `--max-attempts`, `--max-files`, `--retry-ui-errors`, `--start-at`. Не используйте `--retry-deferred` без понимания, что он включает ручные 404.

## 6. Сохранить иконки GPT

```powershell
python scripts/download_custom_gpt_icons_v2_safe.py --plan-only
python scripts/download_custom_gpt_icons_v2_safe.py --attach --pause-min 15 --pause-max 30
```

В тестовом кейсе пользователь подтвердил, что **все иконки** были скачаны. Иконки профиля **не входят** в Knowledge: ими нельзя закрывать недостающие изображения Knowledge.

## 7. Офлайн-аудит

```powershell
python tools/archive_cli.py audit
python scripts/prepare_remaining_library_phase2.py
```

Результаты: `Custom_GPT_backup/reports/offline_audit/{summary.json,knowledge_audit.csv}` и `Custom_GPT_backup/Library_phase2_plan/missing_all.csv`.

Три разные категории: **DIRECT_HASH_VERIFIED** (локальный прямой receipt + SHA подтвердились), **PRESENT_UNVERIFIED...** (имя/размер совпадают, но не доказан оригинал целевого GPT), **MISSING**.

## 8. Общие базы Knowledge

```powershell
python scripts/audit_shared_gpt_knowledge.py --snapshot Custom_GPT_backup/gpt_list_snapshot.json --backup Custom_GPT_backup
python scripts/download_direct_gpt_knowledge_ui_v11.py --all-gpts --stage-shared-candidates
```

Если документы в нескольких GPT имеют одинаковое имя, размер, тип, дату создания и изменения — это **кандидат общего источника**. Совпадение метаданных не доказывает совпадение содержимого. Файлы-доноры сохраняются отдельно в `Shared_Knowledge_candidates`. Даже если они затем устанавливаются в Knowledge целевого GPT, сохраняйте статус *не подтверждено для целевого file_id*.

## 9. Восстановление из Obsidian (исходник только читается)

```powershell
python scripts/auto_restore_knowledge_from_obsidian.py --vault "X:\Vault\GPT_Assistents"
python scripts/auto_restore_knowledge_from_obsidian.py --vault "X:\Vault\GPT_Assistents" --commit
```

Работает со списком **недостающих** файлов; сначала копирует во временный локальный входной каталог `My Downloads Arhiv`, потом в папку GPT. Не меняет исходный Obsidian. При неоднозначных копиях создаёт `04_РУЧНОЙ_ВЫБОР.csv` для ручного выбора. Для подтверждённых одинаковых исходных файлов можно использовать `resolve_obsidian_shared_by_sha.py` (проверка SHA источников; по-прежнему кандидат для GPT).

## 10. Восстановление из любых локальных папок

```powershell
python tools/archive_cli.py import-folder --source "Custom_GPT_backup\My Downloads Arhiv final"
python tools/archive_cli.py import-folder --source "Custom_GPT_backup\My Downloads Arhiv final" --commit
```

По умолчанию только однозначные пары по имени+размеру. При конфликте SHA **отказ**. Если *одни и те же байты* подходят нескольким GPT и вы осознанно хотите расставить кандидатные копии:

```powershell
python tools/archive_cli.py import-folder --source "Custom_GPT_backup\My Downloads Arhiv final" --commit --allow-shared-targets
```

Оригиналы не перезаписываются; локальный архив не изменяется. Отчёты и журнал происхождения пишутся в `Custom_GPT_backup/`.

## 11. Library — только как необязательный экспериментальный этап

В тестовом кейсе UI-поиск Library выдал 15 `NOT_FOUND_UNCONFIRMED` и затем HTTP 429, а **одинаковое имя** могло вести к файлу с **другим содержимым**. `experimental/download_library_custom_gpt_ui_v16_guarded.py` присутствует для диагностики, а не как рекомендуемый основной канал. Не перезаписывайте им файлы GPT и не называйте скачанные им документы «оригиналами» без независимого подтверждения.

## 12. Конец: контроль результата

```powershell
python tools/archive_cli.py audit
python scripts/prepare_remaining_library_phase2.py
```

Стремимся к `missing: 0`, но **физическая полнота != доказанная оригинальность всех экземпляров**. Отдельно просмотрите `PRESENT_UNVERIFIED_OR_ARCHIVE_CANDIDATE`, журналы копий и файлы, которые всё ещё отсутствуют.

**При публикации кода:** только скрипты/обезличенные тесты; никакие пользовательские данные, журналы или browser profiles в GitHub не отправляются.
