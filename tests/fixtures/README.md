# Synthetic test fixtures

Names in fixtures and tests are invented words. No correspondence table with
real people is included. Test phone numbers and credentials are synthetic and
must never be used to contact people or access services.

Do not add real WhatsApp exports, client workbooks, catalogs, aliases, reports,
or credentials. Create any files required by a test inside pytest's temporary
directory. Unicode input fixtures intentionally include accented and international
characters to exercise normalization and export parsing.
