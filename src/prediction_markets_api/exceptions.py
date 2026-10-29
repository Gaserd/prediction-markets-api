"""Exception hierarchy for prediction markets API."""


class PredictionMarketsError(Exception):
    """Base exception for all prediction markets API errors."""


class NotFoundError(PredictionMarketsError):
    """Resource not found (e.g., market, token, outcome)."""

    def __init__(
        self, message: str, *, resource_type: str | None = None, resource_id: str | None = None
    ):
        """Initialize NotFoundError.

        Args:
            message: Error message
            resource_type: Type of resource (e.g., "market", "token")
            resource_id: ID of the resource that was not found
        """
        super().__init__(message)
        self.resource_type = resource_type
        self.resource_id = resource_id


class VenueAPIError(PredictionMarketsError):
    """Error from venue API (HTTP errors, timeouts, invalid responses)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        venue: str | None = None,
        url: str | None = None,
    ):
        """Initialize VenueAPIError.

        Args:
            message: Error message
            status_code: HTTP status code if applicable
            venue: Venue name (e.g., "polymarket", "kalshi", "limitless")
            url: URL that was requested
        """
        super().__init__(message)
        self.status_code = status_code
        self.venue = venue
        self.url = url

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.venue:
            parts.append(f"venue={self.venue}")
        if self.status_code:
            parts.append(f"status={self.status_code}")
        if self.url:
            parts.append(f"url={self.url}")
        return " | ".join(parts)


class RateLimitError(VenueAPIError):
    """Rate limit exceeded (HTTP 429)."""

    def __init__(
        self,
        message: str,
        *,
        venue: str | None = None,
        url: str | None = None,
        retry_after: int | None = None,
    ):
        """Initialize RateLimitError.

        Args:
            message: Error message
            venue: Venue name
            url: URL that was requested
            retry_after: Seconds to wait before retrying (from Retry-After header)
        """
        super().__init__(message, status_code=429, venue=venue, url=url)
        self.retry_after = retry_after


class NoLiquidityError(PredictionMarketsError):
    """No liquidity available (empty order book for an existing market)."""

    def __init__(
        self,
        message: str,
        *,
        market_id: str | None = None,
        side: str | None = None,
    ):
        """Initialize NoLiquidityError.

        Args:
            message: Error message
            market_id: Market identifier
            side: Order side (BUY/SELL) if applicable
        """
        super().__init__(message)
        self.market_id = market_id
        self.side = side


class AuthenticationError(PredictionMarketsError, PermissionError):
    """Missing credentials, or the venue rejected the caller (HTTP 401/403).

    Subclasses the builtin ``PermissionError`` as well as
    ``PredictionMarketsError``, so an existing ``except PermissionError``
    handler still catches it. No separate deprecated alias is defined.

    Attributes:
        venue: Venue name (for example ``"polymarket"``), if known.
        status_code: HTTP status when the venue responded, otherwise None.
        hint: Remediation text, such as which env vars to set or that a
            Limitless token needs the ``trading`` scope.
    """

    def __init__(
        self,
        message: str,
        *,
        venue: str | None = None,
        status_code: int | None = None,
        hint: str | None = None,
    ) -> None:
        """Initialize AuthenticationError.

        Args:
            message: Error message
            venue: Venue name (e.g., "polymarket", "kalshi", "limitless")
            status_code: HTTP status code if the venue responded
            hint: How to fix the failure (env vars, token scope)
        """
        super().__init__(message)
        self.venue = venue
        self.status_code = status_code
        self.hint = hint

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.venue:
            parts.append(f"venue={self.venue}")
        if self.status_code:
            parts.append(f"status={self.status_code}")
        if self.hint:
            parts.append(f"hint={self.hint}")
        return " | ".join(parts)
