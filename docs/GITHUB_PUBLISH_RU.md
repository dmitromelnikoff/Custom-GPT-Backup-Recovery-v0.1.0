# Публикация в GitHub — без раскрытия своего архива

## Сначала убедитесь, что в папке проекта **нет приватных данных**

Публикуем **только** `scripts/`, `tools/`, `experimental/`, `tests/`, `docs/`, `examples/` и служебные файлы проекта. **Не** публикуем: `Custom_GPT_backup/`, `gpt_list_snapshot.json`, `gpts/`, `My Downloads Arhiv*`, *.jsonl, cookies, Chrome profiles, действительные Instructions и Knowledge, бинарные иконки, сохранённые signed URLs, результаты диагностики.

Для публичного GitHub **отдельно** скопируйте содержимое этого релизного ZIP в пустой каталог. Не превращайте всю рабочую папку с `Custom_GPT_backup` в Git-репозиторий.

## Команды

```powershell
# Находиться в КОРНЕ нового каталога, содержащего README.md и .gitignore
python -m unittest discover -s tests -v
python tools/check_public_release.py
python -m compileall -q scripts tools experimental

git init
git add .
git status --short
# ПРОВЕРЬТЕ перечисленные файлы вручную, прежде чем делать commit!
git commit -m "Initial public release: Custom GPT backup and recovery toolkit"
git branch -M main
```

Дальше создайте **пустой** репозиторий в GitHub. Лучше сначала **Private**, провести повторную проверку файлов, и лишь затем сделать **Public** по собственному решению. После создания получите адрес и выполните:

```powershell
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

Адрес замените настоящим (не вставляйте сюда токены). Можно воспользоваться интерфейсом GitHub Desktop вместо командной строки.

## Контроль перед открытой публикацией

```powershell
git ls-files
```

В списке **не должны** находиться `Custom_GPT_backup`, `gpt_list_snapshot`, приватные JSON/CSV, `Instructions.md` реальных GPT и т.п. Не полагайтесь исключительно на `.gitignore`: он не защищает файлы, уже добавленные с `git add -f`.

Сканирование регулярными выражениями не гарантирует обнаружения всех секретов. Полезно дополнительно провести secret scanning и ручную проверку истории git. Если секрет попал в историю, удаление из текущего файла **недостаточно** — историю нужно очистить и соответствующие учетные данные отозвать.

## Репозиторий не должен обещать больше, чем доказано

- Не утверждать, что исходное API является официально поддерживаемым.
- Не утверждать, что получены все 1 869 **оригиналов**; последний аудит: **1 865 файлов на месте / 1 676 direct receipt hash / 189 архивных или size-only копий / 4 отсутствуют**.
- Не обещать гарантированный экспорт Actions или полного runtime состояния GPT.
- Не продвигать автоматическое продолжение после HTTP 401/403/429.

## Рекомендуемые поля GitHub

- **Description:** `Unofficial local-first Custom GPT Knowledge & icon backup, provenance audit and Obsidian recovery toolkit (Windows/Playwright)`
- **Topics:** `custom-gpt`, `backup`, `playwright`, `knowledge-base`, `obsidian`, `data-recovery`, `python`, `provenance`
- **License:** MIT
- **Release tag:** `v0.1.0` (case-study reproducibility release)
