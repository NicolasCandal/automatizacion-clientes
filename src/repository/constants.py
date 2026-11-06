"""Legacy constants derived from the current spreadsheet structure."""

from client_automation.infra.excel_client_reader import (
    EXPECTED_SHEETS as EXPECTED_SHEETS,
)
from client_automation.infra.excel_client_reader import (
    GREEN_RGB,
)
from client_automation.infra.excel_client_reader import (
    WEEK_HEADER_ROW as WEEK_HEADER_ROW,
)

COL_NAME = 1
GREEN_XLSX = "FF" + GREEN_RGB
GREEN_SHEETS = {"red": 118 / 255, "green": 146 / 255, "blue": 60 / 255}
WHITE_XLSX = "FFFFFFFF"
WHITE_SHEETS = {"red": 1.0, "green": 1.0, "blue": 1.0}
COLOR_TOLERANCE = 0.01
LEGACY_TEXT_MARK = "x"
