"""Bounded failures do not reveal protected resource metadata."""


class GolError(Exception):
    """The admitted GOL operation could not be completed."""


class GolDenied(GolError):
    """An authority or provenance obligation was not satisfied."""


class GolUnavailable(GolError):
    """No usable immutable runtime projection or decision was available."""
