# Резервное копирование и восстановление Custom GPT

**Рабочий проект, собранный по итогам фактического восстановления 250 пользовательских GPT.** Код, операционные инструкции, аудит и журнал решений отделены от личных данных.

> **Неофициальный инструмент.** Веб-интерфейс ChatGPT и неофициальные endpoint'ы могут меняться. Работайте только со своими GPT. Не публикуйте `Custom_GPT_backup` и авторизованный профиль браузера.

## Достигнуто в реальном проекте

| Контрольная точка | Значение |
|---|---:|
| GPT в snapshot | 250 |
| Knowledge-записей | 1 869 |
| Файлов на месте и с совпадающим размером | **1 865** |
| Подтверждены журналами прямого скачивания с SHA-256 | **1 676** |
| Копии из Library / Obsidian / других архивов без доказанной идентичности экземпляру GPT | **189** |
| Физически отсутствовали при последнем аудите | **4 изображения Knowledge** |
| Иконки GPT | **Владелец сообщил, что все скачаны** |

**98,9%** — физическая полнота Knowledge по ожидаемому размеру, а **не** процент доказанных оригиналов. 1 676 — проверка SHA-256 относительно локальных журналов получения, а не внешнего эталонного хеша OpenAI.

## Что реально работало

**Основная линия:**

1. Получить и хранить приватный `gpt_list_snapshot.json` (названия, GPT ID, Knowledge file ID, размеры, порядок; иногда Instructions/Actions).
2. Подключить Playwright к **уже авторизованному Chrome** через локальный CDP.
3. Для каждого GPT открыть **редактор → «Конфигурация» → «Знания»**, нажать **кнопку конкретного файла**, скачать из открывшегося просмотрщика.
4. Сохранить SHA-256, происхождение, GPT ID, file ID, ошибки; **не переписывать** существующие Knowledge.
5. Продолжать после ошибок отдельных файлов, но останавливаться при **401/403/429**.
6. Создать список остатка. Поискать его в своих архивах Obsidian/Downloads и установить как **кандидатные копии** с журналом.
7. Отдельно скачать **аватары GPT**.
8. Сделать офлайн-аудит по всем 1 869 записям.

**Доказательство на практике:** два длительных запуска скачали соответственно **1 306** и **339** Knowledge. Кроме того, работали короткие прогоны, восстановление из Obsidian и загрузка иконок.

## Начать с нуля

**Полное пошаговое руководство:** [docs/PLAYBOOK_RU.md](docs/PLAYBOOK_RU.md).

Из каталога проекта:

```powershell
python -m pip install -r requirements.txt

# Имеется приватный snapshot, храним только локально
python scripts/download_direct_gpt_knowledge_ui_v11.py --all-gpts --plan-only

# Пробный запуск — не более одного файла
python scripts/download_direct_gpt_knowledge_ui_v11.py --all-gpts --attach --confirm-access-restored --max-gpts 1 --max-attempts 1 --max-files 1

# Массовый обход при нормальном доступе
python -u scripts/download_direct_gpt_knowledge_ui_v11.py --unattended --attach --confirm-access-restored --pause-min 15 --pause-max 30

# Проверка достигнутого
python tools/archive_cli.py audit
```

**Важно о «с нуля»:** получение исходного списка GPT через неофициальные веб-эндпоинты — исторически сработавший, но уязвимый для изменения интерфейса этап. Он вынесен в `experimental/bootstrap_snapshot_legacy.py`. При его отказе получить снимок следует из другого разрешённого пользователю источника и привести к [структуре snapshot](docs/DATA_MODEL_RU.md). Нельзя обещать гарантированное извлечение GPT из любой версии ChatGPT. Если есть свой полный CSV-реестр, `tools/snapshot_from_csv.py` конвертирует его офлайн (см. `examples/sample_inventory.csv`).

### Обнаружение недостающих документов в локальном Obsidian

```powershell
# Без записи в хранилище — предварительная проверка
python scripts/auto_restore_knowledge_from_obsidian.py --vault "X:\Vault\GPT_Assistents"

# Копировать в backup, НЕ изменяя сам Vault
python scripts/auto_restore_knowledge_from_obsidian.py --vault "X:\Vault\GPT_Assistents" --commit
```

### Если документы найдены вручную в Downloads

```powershell
python tools/archive_cli.py import-folder --source "Custom_GPT_backup\My Downloads Arhiv final"
python tools/archive_cli.py import-folder --source "Custom_GPT_backup\My Downloads Arhiv final" --commit
python tools/archive_cli.py audit
```

Если один и тот же подтверждённый по SHA-256 исходный файл должен быть кандидатом для нескольких GPT, можно добавить `--allow-shared-targets`. Это **не доказывает**, что экземпляры на стороне ChatGPT имели такие же байты.

### Иконки

```powershell
python scripts/download_custom_gpt_icons_v2_safe.py --plan-only
python scripts/download_custom_gpt_icons_v2_safe.py --attach --pause-min 15 --pause-max 30
```

Иконка профиля GPT (`icon.png`) — **не то же самое**, что изображения, прикреплённые в Knowledge.

## Где смотреть

- [Архитектура и поток данных](docs/ARCHITECTURE_RU.md)
- [История и цифры](docs/CASE_STUDY_RU.md)
- [Пошаговая повторяемая схема](docs/PLAYBOOK_RU.md)
- [Сработало / не сработало](docs/DECISIONS_RU.md)
- [Семантика SHA и происхождения](docs/PROVENANCE_RU.md)
- [Диагностика ошибок](docs/TROUBLESHOOTING_RU.md)
- [Модель входного snapshot](docs/DATA_MODEL_RU.md)
- [Что проверено кодом и тестами](docs/TESTING_RU.md)
- [План завершения и открытые вопросы](docs/ROADMAP_RU.md)
- [Подготовка и публикация в GitHub](docs/GITHUB_PUBLISH_RU.md)

## Структура проекта

```text
scripts/               Проверенные инструменты восстановления
experimental/          Недокументированные / не подтвердившиеся методы
tools/archive_cli.py   Новый единый офлайн-аудит и импорт кандидатов
tests/               Автоматические тесты на синтетических данных
docs/                Решения, ограничения и инструкции
examples/            Только обезличенный пример snapshot
Custom_GPT_backup/  Личные данные владельца — ИГНОРИРУЮТСЯ GIT
```

Ни один приватный GPT, Instructions, Knowledge, профиль Chrome, подписанная ссылка или токен не включены в репозиторий.
