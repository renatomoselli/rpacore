# Export Format Reference

RPA Core exports persisted transactions as JSON or NDJSON through:

```bash
rpacore transaction export --format json
rpacore transaction export --format ndjson
```

## JSON

JSON export writes one envelope:

```json
{
  "export_format_version": 1,
  "framework_version": "0.1.0",
  "exported_at": "2026-01-01T00:00:00+00:00",
  "transactions": []
}
```

Each entry in `transactions` is the canonical serialized transaction record and
contains `transaction_format_version`.

## NDJSON

NDJSON export writes one JSON object per line. Each line contains
`export_format_version`, `framework_version`, `exported_at`, and a transaction
record. Consumers should parse each line independently.

## Sensitive Data

Machine-readable transaction records can include user-supplied state, metadata,
skill arguments, exception messages, and artifact metadata. They do not include
runtime resources, config, credentials, or artifact file contents.

Export files should be handled as sensitive business data.
