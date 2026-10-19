"""Exception hierarchy for prediction markets API."""


class PredictionMarketsError(Exception):
    """Base exception for all prediction markets API errors."""


class NotFoundError(PredictionMarketsError):
    """Resource not found (e.g., market, token, outcome)."""

    def __init__(self, message: str, *, resource_type: str | None = None, resource_id: str | None = None):
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
