# data/

| Folder | Content | In Git |
|---|---|---|
| `database/` | Local SQLite databases, migration backups | No (clinical data, audit trail) |
| `runtime/tmp_audio/` | Temporary audio, deleted per retention policy | No |
| `documents/` | Project documentation (status, database, assistant, compliance matrix, proposal notes) | Yes |
| `knowledge/` | Curated clinical reference passages, term map, source manifest | Yes |
| `samples/` | Fictional examples only | Yes |
| `evaluation/` | Evaluation script and actual results on fictional cases | Yes |

Never place real patient data, API keys or passwords in this folder outside `database/` and `runtime/`.
