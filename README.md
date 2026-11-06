# Client Automation

Import weekly activity from exported WhatsApp TXT files, match contacts against
an Excel workbook or Google Sheets document, and preview or apply weekly client
marks.

The import stores declared identity, optional phone number, the last activity
date, and group status. It does not extract commercial information from message
bodies. Chat import and spreadsheet registration are separate operations.

No real client records or credentials are distributed. Tests and examples use
synthetic identities. Local inputs, state, backups, configuration, and credentials
are excluded from Git and distribution archives.

## Project structure

```text
.
├── pyproject.toml
├── poetry.lock
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
├── .github/workflows/main.yml
├── README.md
├── docs/
│   └── architecture.md
├── examples/whatsapp/
│   ├── manifest.json
│   ├── contact_demo.txt
│   └── group_demo.txt
├── src/
│   ├── client_automation/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── config.py
│   │   ├── main.py
│   │   ├── core/
│   │   ├── infra/
│   │   ├── services/
│   │   └── utils/
│   ├── catalog_index/
│   ├── matching/
│   └── repository/
├── typings/
└── tests/
    ├── fixtures/
    ├── unit/
    └── integration/
```

Input, state, output, and backup directories are configurable and are not included
in the package. [Architecture and guarantees](docs/architecture.md) describes the
contracts, validation rules, retry behavior, and adapter limitations.
The collection-based facades delegate to the main implementation.

## Installation

Use Python 3.12, 3.13, or 3.14 and Poetry. CI pins Poetry 2.3.1.

```console
poetry sync --with dev
poetry run client-automation --help
```

Copy `.env.example` to `.env` and set your own configuration.
Pydantic Settings validates all `CA_` environment variables. Importing a module
does not load a dotenv file or perform application operations.

Relative paths resolve against `CA_BASE_DIRECTORY`.
Changing `CA_DATA_DIRECTORY` does not change the other explicit paths.
Keep separate state and backups for every workbook or document.

Dry run is the default. `--dry-run` and `--apply` override `CA_DRY_RUN`.
Dry runs do not save application files or change marks. Sheets dry runs may query
the API and refresh credentials in memory without persisting the refreshed token.

## 1. Prepare exported chats

Export each conversation as UTF-8 TXT and declare its identity in a manifest.
[examples/whatsapp](examples/whatsapp) contains a complete synthetic example.

Each entry declares its file, raw name, optional phone number, exact sender
labels, and group status. Date order is explicit. Identities are never inferred
from filenames. TXT paths must remain inside the manifest directory.

The period is inclusive. The import selects the last activity within that
period, even when later messages exist. A chat without activity in the period
produces no contact. An empty week is valid.

```console
poetry run client-automation --import-whatsapp --start-date 2026-11-02 --end-date 2026-11-06 --manifest data/input/whatsapp/manifest.json --dry-run
```

After reviewing the batch, save the weekly input:

```console
poetry run client-automation --import-whatsapp --start-date 2026-11-02 --end-date 2026-11-06 --manifest data/input/whatsapp/manifest.json --apply
```

Import writes only `CA_CONTACTS_PATH`. It does not open the workbook, authorize
Google, or register marks. The full batch is validated before saving.
Message bodies are not retained in weekly output.

## 2. Configure the destination

### Excel

Set `CA_REPOSITORY=excel` and configure `CA_EXCEL_PATH`.
The workbook must contain `Inviu`, `IOL`, `Bull`, `Balanz`, and `PPI`.
A1 identifies the client column. Client names start in A3 and weekly headers
occupy row 2. Weekly columns start in C, except Balanz, which starts in D.

The client column accepts Client, Clients, or Name headers, including supported
localized variants.

A green RGB `118,146,60` fill or literal `x` counts as a mark. Applying changes
makes selected client cells green and other client cells white in the chosen
weekly column. Unsupported structures are rejected; review adapter restrictions
before using an operational workbook.

### Google Sheets

Set `CA_REPOSITORY=sheets`, `CA_SPREADSHEET_ID`, and your credential paths.
The document follows the same five-sheet structure as Excel.

Initial OAuth consent is a separate operation:

```console
poetry run client-automation --authorize-google
```

This opens a browser and saves the token. Other commands never start consent
automatically or delete a token after an authentication failure.
The default scope permits reading only. Applying changes requires
`CA_GOOGLE_SCOPES=["https://www.googleapis.com/auth/spreadsheets"]`,
appropriate consent, and effective document permissions.

Service accounts are supported through
`CA_GOOGLE_AUTHENTICATION_TYPE=service_account`; they use their own credentials
and document permissions without interactive OAuth consent.

Keep credentials and tokens outside source code in the configured `secrets/`
paths. Their JSON files are not encrypted by the application.

## 3. Resolve and register a week

Registration reads `CA_CONTACTS_PATH`, builds or refreshes the source catalog,
resolves contacts, and plans the weekly mark replacement.

```console
poetry run client-automation --week-header "2026-11-02 to 2026-11-06" --dry-run
```

Headers are opaque labels. Use the exact text of an existing week or a new
label that includes its year. The application does not interpret header dates.

Pending contacts block registration by default. Applying a blocked plan saves
the catalog and report for review, without learning aliases or creating a
backup. `--allow-pending` explicitly permits replacement using resolved contacts
only, including any unmarking implied by that replacement.

```console
poetry run client-automation --week-header "2026-11-02 to 2026-11-06" --apply
```

When changes exist, the workflow creates a backup, checks source revisions, and
verifies the result. Excel backups retain the original bytes exactly.
Sheets backups are local JSON snapshots of the queried fields of the five
required sheets. They exclude other sheets, permissions, and history.

Use one writer per source and state directory. There is no transaction spanning
auxiliary files and spreadsheets, nor across sheets. Automatic restoration is
not implemented. Interrupted operations may have applied some marks.
A dry run does not guarantee support for every destination feature checked by
the writer during application.

## Options and exit codes

`--env-file PATH` selects a configuration file; `--no-env` disables dotenv loading.
`--base-directory PATH` overrides the path base.
`--rebuild-catalog` rebuilds an invalid cache from the source; IDs that exist
only in a lost cache cannot be recovered.

| Code | Meaning |
| --- | --- |
| 0 | Completed operation or help |
| 1 | Unexpected failure |
| 2 | Invalid arguments, configuration, or domain input |
| 3 | Registration blocked by pending contacts |
| 4 | Infrastructure, authentication, or permission failure |
| 5 | Concurrent source or state modification |
| 6 | Interrupted registration |
| 130 | User interruption |

Diagnostics use standard logging with timestamps, levels, and context.
Application code does not call `print()`.

## Development

```console
poetry check --lock
poetry run ruff check src typings tests
poetry run ruff format --check src typings tests
poetry run mypy
poetry run pytest --cov=client_automation --cov-report=term-missing
poetry build
```

Strict mypy and Ruff include every package and test. Local pre-commit hooks run
the same formatting and type checks. GitHub Actions runs tests and builds on
Windows/Linux with Python 3.12, 3.13, and 3.14. It does not deploy, publish, or
run the automation against live client data.

Tests use synthetic identities, temporary workbooks, and mocked HTTP.
Unicode and locale-specific input fixtures exercise international exports.
Before operational use, validate the workflow with your own chat export and a
copy of the destination. Local tests do not verify live OAuth consent or remote CI.

