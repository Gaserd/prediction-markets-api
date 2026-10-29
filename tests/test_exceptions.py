"""Unit tests for the trading exception hierarchy. No network.

``AuthenticationError`` subclasses ``PermissionError``, so no deprecated alias
is defined. Nothing in this module was renamed.
"""

import inspect

import pytest

import prediction_markets_api as api
from prediction_markets_api.exceptions import (
    AuthenticationError,
    OrderRejectedError,
    PredictionMarketsError,
    VenueAPIError,
)


def test_authentication_error_inheritance_and_attributes():
    """AuthenticationError is both a library error and a builtin PermissionError."""
    assert issubclass(AuthenticationError, PredictionMarketsError)
    assert issubclass(AuthenticationError, PermissionError)
    assert not issubclass(AuthenticationError, VenueAPIError)

    err = AuthenticationError(
        "missing credentials",
        venue="limitless",
        status_code=403,
        hint="LIMITLESS token needs the trading scope",
    )
    assert isinstance(err, AuthenticationError)
    assert isinstance(err, PredictionMarketsError)
    assert isinstance(err, PermissionError)
    assert not isinstance(PermissionError("nope"), AuthenticationError)
    assert err.venue == "limitless"
    assert err.status_code == 403
    assert err.hint == "LIMITLESS token needs the trading scope"
    assert err.args == ("missing credentials",)
    text = str(err)
    assert "missing credentials" in text
    assert "venue=limitless" in text
    assert "status=403" in text
    assert "hint=LIMITLESS token needs the trading scope" in text


def test_except_permission_error_catches_authentication_error():
    """Existing handlers written against PermissionError keep working."""
    try:
        raise AuthenticationError("missing credentials", venue="kalshi")
    except PermissionError as exc:
        assert isinstance(exc, AuthenticationError)
        assert isinstance(exc, PredictionMarketsError)
        assert exc.venue == "kalshi"
    else:
        raise AssertionError("except PermissionError did not catch AuthenticationError")

    try:
        raise AuthenticationError("unauthorized", status_code=401)
    except PredictionMarketsError as exc:
        assert isinstance(exc, AuthenticationError)
        assert exc.status_code == 401
    else:
        raise AssertionError("except PredictionMarketsError did not catch AuthenticationError")


def test_authentication_error_optional_fields_and_keyword_only():
    """Venue details are optional. Extra positional arguments are rejected."""
    err = AuthenticationError("no keys")
    assert err.venue is None
    assert err.status_code is None
    assert err.hint is None
    assert str(err) == "no keys"

    with pytest.raises(TypeError):
        AuthenticationError(*("no keys", "kalshi"))

    parameters = inspect.signature(AuthenticationError.__init__).parameters
    assert not any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())


def test_order_rejected_error_inheritance_and_venue_code():
    """OrderRejectedError is a VenueAPIError and keeps the venue rejection code."""
    assert issubclass(OrderRejectedError, VenueAPIError)
    assert issubclass(OrderRejectedError, PredictionMarketsError)
    assert not issubclass(OrderRejectedError, AuthenticationError)
    assert not issubclass(OrderRejectedError, PermissionError)

    err = OrderRejectedError(
        "duplicate client order id",
        status_code=409,
        venue="kalshi",
        url="https://example.test/orders",
        venue_code="duplicate_client_order_id",
    )
    assert isinstance(err, OrderRejectedError)
    assert isinstance(err, VenueAPIError)
    assert isinstance(err, PredictionMarketsError)
    assert err.status_code == 409
    assert err.venue == "kalshi"
    assert err.url == "https://example.test/orders"
    assert err.venue_code == "duplicate_client_order_id"
    text = str(err)
    assert "duplicate client order id" in text
    assert "venue=kalshi" in text
    assert "status=409" in text
    assert "url=https://example.test/orders" in text
    assert "venue_code=duplicate_client_order_id" in text

    plain = OrderRejectedError("market closed")
    assert plain.venue_code is None
    assert plain.status_code is None
    assert str(plain) == "market closed"

    parameters = inspect.signature(OrderRejectedError.__init__).parameters
    assert not any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    with pytest.raises(TypeError):
        OrderRejectedError(*("market closed", "closed"))


def test_trading_errors_are_exported_from_the_package_root():
    """Both new errors are importable from the package root, with no alias."""
    assert api.AuthenticationError is AuthenticationError
    assert api.OrderRejectedError is OrderRejectedError
    assert "AuthenticationError" in api.__all__
    assert "OrderRejectedError" in api.__all__
    assert not hasattr(api, "AuthError")
