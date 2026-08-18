"""Custom exceptions for SentinelIQ."""


class SentinelIQError(Exception):
    """Base class for all SentinelIQ errors."""


class DocumentLoadError(SentinelIQError):
    """A document could not be opened, parsed, or yielded no usable text."""


class EdgarFetchError(SentinelIQError):
    """A SEC EDGAR request failed or returned nothing usable."""
