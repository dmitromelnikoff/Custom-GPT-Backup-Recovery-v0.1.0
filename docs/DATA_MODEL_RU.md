# Ожидаемая форма snapshot

Основной загрузчик использует локальный JSON, полученный владельцем разрешённым способом. Формат соответствует зафиксированному историческому `gpt_list_snapshot.json`.

```json
{
  "gpts": [
    {
      "resource": {
        "gizmo": {
          "id": "g-EXAMPLE001",
          "display": {"name": "Demo GPT", "profile_pic_id": null, "profile_picture_url": null}
        },
        "files": [
          {"file_id": "file-EXAMPLE001", "name": "readme.md", "size": 11,
           "type": "text/markdown", "created_at": "2026-01-01T00:00:00Z",
           "last_modified": "2026-01-01T00:00:00Z"}
        ]
      }
    }
  ]
}
```

Снимок хранится в **`Custom_GPT_backup/gpt_list_snapshot.json`**, который игнорируется Git. Snapshot **содержит приватные метаданные** и иногда Instructions/Actions. Публичный `examples/sample_snapshot.json` — полностью синтетический, не экспорт пользователя.

Раскладка Knowledge (порядок из snapshot значим):

```text
Custom_GPT_backup/
  gpt_list_snapshot.json
  gpts/
    Demo GPT/
      g-EXAMPLE001/
        knowledge/
          01_readme.md
        icon.png
  direct_gpt_ui_knowledge_inventory.csv
  direct_gpt_deferred_originals.json
  Shared_Knowledge_candidates/
  My Downloads Arhiv/
  reports/
```

Не меняйте порядок `files[]` в существующем snapshot во время восстановления: ordinal входит в ожидаемое имя файла на диске. В новой версии системы для долговременной устойчивости желательно перейти на путь, привязанный к `file_id`, но для совместимости с рабочим архивом здесь сохранён исторический формат.
