"""Error classification contracts for orchestration and retries."""

from datetime import date

import pytest

from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    AutomationError,
    ConcurrencyError,
    ConfigurationError,
    DomainError,
    DomainValidationError,
    ExternalServiceError,
    InfrastructureError,
    OperationTimeoutError,
    PersistenceError,
    StructureError,
    TransientError,
)
from client_automation.core.models import WeeklyPeriod


def test_models_raise_error_of_domain_compatible_with_value_error() -> None:
    with pytest.raises(AutomationError) as capture:
        WeeklyPeriod(start_date=date(2026, 11, 6), end_date=date(2026, 11, 2))

    assert isinstance(capture.value, DomainValidationError)
    assert isinstance(capture.value, DomainError)
    assert isinstance(capture.value, ValueError)
    assert capture.value.CODE == "VALIDATION"
    assert not isinstance(capture.value, InfrastructureError)


@pytest.mark.parametrize("kind_error", [TransientError, OperationTimeoutError])
def test_failures_temporary_can_capture_as_service_or_infrastructure(
    kind_error: type[TransientError],
) -> None:
    error = kind_error("The service did not respond in time.")

    assert isinstance(error, AutomationError)
    assert isinstance(error, ExternalServiceError)
    assert isinstance(error, InfrastructureError)
    assert not isinstance(error, DomainError)


@pytest.mark.parametrize(
    "kind_error",
    [
        DomainValidationError,
        StructureError,
        ConcurrencyError,
        ConfigurationError,
        AuthenticationError,
        AuthorizationError,
        PersistenceError,
        ExternalServiceError,
    ],
)
def test_failures_without_classification_temporary_no_enter_in_retries(
    kind_error: type[AutomationError],
) -> None:
    error = kind_error("The operation requires review.")

    assert not isinstance(error, TransientError)


def test_authentication_and_permissions_can_handle_by_separate() -> None:
    without_identity = AuthenticationError("Access must be authorized.")
    without_permissions = AuthorizationError("Permission for the operation is missing.")

    assert isinstance(without_identity, InfrastructureError)
    assert isinstance(without_permissions, InfrastructureError)
    assert not isinstance(without_identity, AuthorizationError)
    assert not isinstance(without_permissions, AuthenticationError)


@pytest.mark.parametrize("kind_error", [TypeError, KeyError, RuntimeError, ValueError, OSError])
def test_handling_of_errors_expected_no_capture_failures_without_translate(
    kind_error: type[Exception],
) -> None:
    assert not isinstance(kind_error("Untranslated technical failure."), AutomationError)


def test_categories_have_codes_different_for_reports() -> None:
    categories = (
        AutomationError,
        DomainError,
        DomainValidationError,
        StructureError,
        ConcurrencyError,
        ConfigurationError,
        InfrastructureError,
        AuthenticationError,
        AuthorizationError,
        PersistenceError,
        ExternalServiceError,
        TransientError,
        OperationTimeoutError,
    )

    codes = tuple(category.CODE for category in categories)

    assert len(set(codes)) == len(categories)
    assert all(code and code.isupper() for code in codes)
