# ESG Sustainability Platform

Enterprise-grade ESG data ingestion and normalization backend built with Django + DRF, with a vanilla-JS frontend.

```
Assignment/
├── backend/          ← Django project
│   ├── esg_platform/ ← Django settings, root URLs, WSGI
│   ├── ingestion/    ← App: models, services, views, serializers, admin
│   ├── manage.py
│   ├── requirements.txt
│   └── smoke_test.py
└── frontend/         ← Static SPA (no build step)
    ├── index.html
    ├── styles.css
    └── app.js
```

---

## Backend Setup

```bash
cd backend
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # optional, for /admin
python manage.py runserver
```

The API is available at `http://127.0.0.1:8000/api/`.

### Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| `POST` | `/api/ingest/` | Upload file or raw payload |
| `GET`  | `/api/dashboard/records/` | List normalized records (filterable) |
| `POST` | `/api/records/<id>/approve/` | Approve → immutable |
| `POST` | `/api/records/<id>/reject/` | Reject a record |
| `PATCH`| `/api/records/<id>/edit/` | Correct original_value (writes AuditLog) |

### Ingest — example cURL calls

**SAP Export (Scope 1)**
```bash
curl -X POST http://127.0.0.1:8000/api/ingest/ \
  -F "source_type=SAP_EXPORT" \
  -F "tenant_id=<YOUR_TENANT_UUID>" \
  -F "file=@sap_export.csv"
```

**Utility CSV (Scope 2)**
```bash
curl -X POST http://127.0.0.1:8000/api/ingest/ \
  -F "source_type=UTILITY_CSV" \
  -F "tenant_id=<YOUR_TENANT_UUID>" \
  -F "file=@utility_bill.csv"
```

**Corporate Travel JSON (Scope 3)**
```bash
curl -X POST http://127.0.0.1:8000/api/ingest/ \
  -F "source_type=CONCUR_JSON" \
  -F "tenant_id=<YOUR_TENANT_UUID>" \
  -F "raw_payload={\"trips\":[{\"type\":\"Flight\",\"origin\":\"JFK\",\"destination\":\"LAX\",\"travel_date\":\"2026-04-15\",\"passengers\":1}]}"
```

### Create a tenant via Django shell

```python
python manage.py shell
>>> from ingestion.models import Organization
>>> org = Organization.objects.create(name="Acme Corp")
>>> print(org.id)   # copy this UUID for API calls
```

---

## Frontend Setup

No build step needed. Open `frontend/index.html` directly in a browser, or serve it with any static file server:

```bash
cd frontend
python -m http.server 3000
# then open http://localhost:3000
```

Make sure the Django dev server is running on port 8000 first.

---

## Database

The project ships with SQLite for development. To switch to PostgreSQL, update `DATABASES` in `backend/esg_platform/settings.py`:

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "esg_db",
        "USER": "postgres",
        "PASSWORD": "yourpassword",
        "HOST": "localhost",
        "PORT": "5432",
    }
}
```

---

## Architecture Notes

- **Multi-tenancy**: every model is scoped to an `Organization` via FK.
- **Data lineage**: every `NormalizedEmissionActivity` carries a `raw_log` FK pointing back to the exact `RawIngestionLog` that produced it.
- **Immutability**: approved records block `model.save()` and all `PUT/PATCH` API calls return `409 Conflict`.
- **Audit trail**: every human edit via `/edit/` writes one `AuditLog` row per changed field.
- **Anomaly detection**: Utility CSV parser compares each row against the historical average for that facility and flags surges > 200%.
- **Emission factors**: hardcoded constants in `ingestion/constants.py` (IPCC AR6 / US EPA / DEFRA 2023 proxies).
