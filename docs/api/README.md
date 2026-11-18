# API reference workbook

`CoGo-API-Reference.xlsx` documents every service. It has these sheets:

- **Overview:** basics, breaking changes, and an index with live counts.
- **Gateway Routes**
- **Auth Service**
- **User Service**
- **Notification Service**
- **Events**
- **Error Codes**
- **Schemas**

Field-level schemas for auth and user are **generated from each service's OpenAPI spec**, so
types and constraints match the code. Endpoint notes, examples, errors and rate limits are
curated in `build_api_xlsx.py`.

## Regenerate after an API change

```bash
OUT=$(mktemp -d)
for svc in auth user; do
  (cd ../../services/$svc && PYTHONPATH=. uv run --quiet python ../../docs/api/dump_openapi.py $svc $OUT/$svc-openapi.json)
done
uv run --no-project --with openpyxl python build_api_xlsx.py $OUT CoGo-API-Reference.xlsx
```

Run these commands from `docs/api/`. Update the curated rows in `build_api_xlsx.py` when you
change or add an endpoint, error type or route. The Overview counts are Excel formulas and
recalculate when the file is opened.
