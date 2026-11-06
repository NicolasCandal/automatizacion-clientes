"""Regression tests for contracts."""

from unittest.mock import create_autospec

import pytest

from client_automation.core.contracts import ClientReader, ClientWriter


def test_write_requires_revision_as_argument_explicit() -> None:
    writer = create_autospec(ClientWriter, instance=True, spec_set=True)
    rows = frozenset({3, 20})

    with pytest.raises(TypeError, match="expected_revision"):
        writer.apply_marks("IOL", 10, "02 on 06-11", rows)
    with pytest.raises(TypeError):
        writer.apply_marks("IOL", 10, "02 on 06-11", rows, "revision_1")

    writer.apply_marks("IOL", 10, "02 on 06-11", rows, expected_revision="revision_1")

    writer.apply_marks.assert_called_once_with(
        "IOL", 10, "02 on 06-11", rows, expected_revision="revision_1"
    )
    writer.create_backup.assert_not_called()


def test_double_of_read_prevents_use_operations_of_write() -> None:
    reader = create_autospec(ClientReader, instance=True, spec_set=True)

    with pytest.raises(AttributeError, match="apply_marks"):
        reader.apply_marks("IOL", 10, "02 on 06-11", frozenset(), expected_revision="revision_1")
    with pytest.raises(AttributeError, match="create_backup"):
        reader.create_backup()
