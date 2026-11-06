# Architecture and guarantees

## Layers

- `core/`: immutable models, structural ports, normalization, broker recognition,
  catalog construction, matching, alias reconciliation, registration plans, and
  TXT activity parsing. Only the Python standard library is used.
- `infra/`: Excel/Sheets I/O, authentication, JSON persistence, TXT manifests,
  RapidFuzz, logging, and the system clock. Adapters receive explicit settings
  and translate external failures into application exceptions.
- `services/`: coordinate catalog, matching, registration, and import through
  injected ports. Services do not construct HTTP clients or parse JSON.
- `utils/`: shared utilities without business rules.
- `main.py`: compose adapters, validate paths and options, and translate outcomes
  into exit codes. Importing it performs no operations.

Ports use `Protocol`. Periods use calendar dates and events use timezone-aware
instants. Original names and all occurrences are preserved, including multiple
rows in one sheet. Normalization cannot distinguish genuine namesakes.

## Matching and persistence

Matching priority is group/exclusion, phone, exact-name alias, exact name, and
fuzzy comparison. Broker hints restrict destinations without resolving ties.
All candidates are compared before displayed suggestions are limited.
Default thresholds are 92 for automatic resolution and 80 for review, configured
through `CA_AUTOMATIC_MATCH_THRESHOLD`, `CA_REVIEW_MATCH_THRESHOLD`, and
`CA_MATCH_SUGGESTION_LIMIT`.

Services share one contact extraction and catalog snapshot. Decisions are
previewed before aliases are learned. Conflicting identities, destinations, and
phone numbers are not silently overwritten. Partial contact resolutions are
not learned. Catalog revisions and IDs belong to a specific source.

A corrupt cache is not treated as empty. `--rebuild-catalog` rebuilds from the
source without recovering IDs that existed only in the invalid cache.
Pending contacts block writes by default. In apply mode, the catalog and report
are saved for review, without learning aliases or creating a backup for the
blocked plan. `--allow-pending` permits replacement using resolved contacts only,
including any resulting unmarking.

The plan is revalidated before auxiliary files are persisted. In apply mode,
the catalog, report, and learned aliases are saved before modifying the source.
A backup is created only when changes exist. Clients, structure, headers, and
marks are verified after each applied sheet. No transaction spans all files or
sheets, so an error may leave auxiliary files saved or some sheets applied.

## Excel

Required sheets are Inviu, IOL, Bull, Balanz, and PPI.
A1 must identify the client/name column. Client rows start at A3.
Weekly headers occupy row 2, with weeks starting in C or D for Balanz.
Gaps are preserved and formula-derived client names are rejected.
Headers are opaque labels rather than parsed dates.

Marks use green RGB 118,146,60 or a literal x. Applying changes sets selected
client cells green and other client cells white, without x values.
Blank client rows, fixed columns, and other weekly columns are not written.
Formulas, unrelated values, merged cells, and protection affecting the
destination are rejected. Unresolved theme/indexed colors prevent mark reads.

Revisions are SHA-256 hashes of workbook bytes. Backups copy those exact bytes
into exclusive, synchronized files. Writes serialize to a closed temporary
file, synchronize it, recheck the revision, then replace the original.
Windows sharing violations 32/33 receive bounded retries.

The writer rejects workbook components that openpyxl cannot safely preserve,
including drawings, macros, controls, slicers, and signatures.
No-op plans do not save or normalize existing marks.

## Google Sheets and authentication

The reader requests all five sheets without a fixed row or ZZ column limit.
The snapshot includes values, formatting, dimensions, merges, protections,
banding, tables, conditional rules, and themes. Effective and theme colors are
resolved. Unresolvable mark colors do not prevent reading client names.

Queries reuse the snapshot, while `revision()` always downloads a fresh one and
hashes the requested JSON. Failed refreshes invalidate the previous snapshot.
Services bracket consistent reads with revision checks.

The writer checks revisions before preparing and before sending each batch.
One `batchUpdate` per sheet assigns values and background fields atomically.
It never inserts or shifts columns; it increases the absolute column count only
when necessary. Contiguous client rows are grouped without filling gaps.
Batches above 1,900,000 bytes are rejected before transmission.

Destination data validation, interfering merges/protection/banding/conditional
rules, tables, pivots, and data sources are rejected. A dry run validates the
plan without guaranteeing support for every feature checked by the writer.

Sheets backups are local JSON containing queried fields of the five sheets.
They exclude other sheets, permissions, history, and unrequested resources.
They are not full Drive copies. Recovery is manual; no automatic restorer exists.

Credential loading uses OAuth or a service account as configured.
Normal loading never starts consent to recover from failure.
Explicit OAuth authorization uses browser consent, PKCE, a 127.0.0.1 callback
with an ephemeral port, and a bounded wait. Refresh may occur in memory;
the CLI persists refreshed credentials only in apply mode.

Prior tokens are not deleted after rejection or revocation.
Configured scopes must be explicitly declared or granted; scopes are not
silently expanded and equivalent URIs are not inferred. Google enforces the
effective permissions.

Credential/token JSON files contain plaintext secrets. On Windows they inherit
directory permissions. Application logs omit secrets. Authentication temporarily
silences already-loaded SDK loggers, so authentication must run sequentially.
Technical exception causes remain available for debugging and may contain
private information.

## Retries and concurrency

Tenacity bounds network operations using explicit timeouts and randomized
exponential waits. Defaults are four attempts, a 30-second request timeout,
and waits between one and 30 seconds. This is not a total workflow deadline.

Transient connection failures, HTTP 408/429, and 5xx except 501/505 are retried.
HTTP 403 is retried only when every structured reason indicates a quota issue.
Invalid TLS, permissions, rejected authentication, and malformed responses fail
without retrying. Transport and SDK retries do not multiply application attempts.

Before resending writes, the source revision is checked. A timeout may occur
after a batch was applied. If the revision changed, writing stops.
An identical revision permits only idempotent assignments to be retried.
Uncertain success is not inferred. Single-use OAuth code exchange retries only
ConnectTimeout, before connection establishment.

Use one writer per source and state. Local hash checks are not atomic server
preconditions and do not lock Sheets. Excel/JSON replacement is not filesystem
compare-and-swap. Concurrent editors and volatile formulas may invalidate
revisions. Full Sheets snapshots consume memory, time, and API quota.

## JSON and TXT import

JSON persistence validates types and finite numbers, writes a temporary file,
synchronizes and closes it, then replaces the destination in the same directory.
A failure before replacement preserves the original. Directory synchronization
and power-loss durability are not guaranteed. Alias and exclusion stores also
check their prior persisted state before replacing it.

Import requires a nonempty schema-version-1 manifest with known fields.
TXT paths must be relative, unique, and contained within the manifest directory,
without parent traversal or alternate streams. ZIP, non-UTF-8, malformed input,
and oversized batches are rejected without returning partial results.
File handles are closed even when parsing fails.

Android separators and iOS brackets are supported, with 12/24-hour times,
optional seconds, LF/CRLF, UTF-8 BOM, and leading LRM/RLM markers.
Day/month order is explicit; ISO YYYY-MM-DD is accepted. Two-digit years require
century 1900 or 2000. Dates are interpreted without timezone conversion.

Only exact, nonambiguous declared sender labels followed by ': ' count.
System events and continuation lines do not generate independent activity.
Message bodies imitating headers may be indistinguishable; exported text is not
authenticated.

Output contains one contact per active file with declared identity and the last
activity date within the inclusive period. Later messages do not erase activity
inside the week. Groups are retained for explicit engine exclusion.
Chats are not merged by name and identities or countries are never inferred.
Empty weeks are valid; review them before replacing destination marks.

Defaults are 500 chats, 25 MiB per TXT, 256 KiB per line, 100 MiB per batch, and
1 MiB per manifest. File identity, size, and timestamp checks detect ordinary
concurrent changes but do not create an atomic snapshot. Inputs must remain
unchanged during import. Completeness of the chat batch/history is not verified.

## Verification

Ruff, strict mypy, and pytest include all packages and tests.
Local google_auth_oauthlib stubs describe only the surface actually used;
they do not change the SDK or suppress broad categories of type checking.
Poetry locks dependencies.

CI builds and tests on Windows/Linux with Python 3.12, 3.13, and 3.14, without
deployment or publication. Tests use synthetic chat exports, temporary workbooks,
mock credentials, and mocked HTTP. They do not prove live OAuth, live Sheets,
or remote CI execution. Perform acceptance testing against your own export
and a destination copy before operational use.
