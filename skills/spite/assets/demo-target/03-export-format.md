Saved page: https://pebblecount.example/docs/export-format

# Export format (version 2)

Your jar, your rules. An export is one JSON document, UTF-8, shaped like this:

```json
{
  "format": "pebblecount-export",
  "version": 2,
  "exported": "2026-03-14",
  "habits": [
    {
      "name": "Morning run",
      "created": "2026-01-02",
      "checkins": ["2026-01-02", "2026-01-03", "2026-01-05"]
    }
  ]
}
```

Field notes:

- `format` is always the text `pebblecount-export`. Importers should refuse files with any other value.
- `version` is the whole number 2. Version 1 files (which used `days` instead of `checkins`) are no longer produced; importers may refuse them.
- `exported` is the date the file was written.
- `habits` is a list, in the order the habits were added.
- `name` is the habit name. `created` is the date the habit was added. `checkins` is a list of dates in ascending order with no repeats.
- All dates are `YYYY-MM-DD`.

Ids are not part of an export. They belong to the jar they live in.
