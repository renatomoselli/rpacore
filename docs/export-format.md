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
  "framework_version": "0.2.0",
  "exported_at": "2026-01-01T00:00:00+00:00",
  "transactions": []
}
```

Each entry in `transactions` is the canonical serialized transaction record and
contains `transaction_format_version`.

## Compatibility

The JSON export envelope, each NDJSON line, and the nested transaction record
are independently versioned. Consumers must check both the export and
transaction version before interpreting a record. Version 1 has a closed set
of framework-owned fields: changing, removing, renaming, retyping, or changing
the meaning of one requires a new format version. RPA Core does not add new
framework-owned fields to an existing export version.

## Iteration Consistency

Export selects records through normalized-UTC cursor pages in `created_at DESC,
id ASC` order. It keeps one page of summaries at a time and releases the page
query before loading records, so slow output or partial consumption does not
hold a SQLite read lock against concurrent checkpoints. Inserts before the
current cursor do not appear later; inserts after it can appear in a later page.
Updates to a selected transaction are visible when that record is loaded. A
selected transaction deleted by concurrent cleanup before its load is omitted.
Export has no transaction-list limit.

## NDJSON

NDJSON export writes one JSON object per line. Each line contains
`export_format_version`, `framework_version`, `exported_at`, and a transaction
record. Consumers should parse each line independently.

## Sensitive Data

Machine-readable transaction records can include user-supplied state, metadata,
skill arguments, exception messages, and artifact metadata. They do not include
runtime resources, config, credentials, or artifact file contents.

Export files should be handled as sensitive business data.
