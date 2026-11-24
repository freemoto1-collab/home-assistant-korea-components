class KepcoApiError(Exception):
    """Base exception for KEPCO API errors."""

    pass


class KepcoAuthError(KepcoApiError):
    """Exception for KEPCO authentication errors."""

    pass


class KepcoCustomerMismatchError(KepcoApiError):
    """Response belongs to a different customer number than the entry selected."""

    pass
