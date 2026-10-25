"""Bound approximate matching without indexing decrypted correspondence on disk."""

from __future__ import annotations

import re
import unicodedata

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(value: str) -> str:
    if value.isascii():
        return value.casefold()
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


class MailSearch:
    """Reuses bounded work buffers across all records in one authorized page."""

    __slots__ = ("terms", "literal", "remaining", "limited", "_rows")

    def __init__(self, query: str) -> None:
        self.literal = normalize(query).strip()
        self.terms = tuple(_WORDS.findall(self.literal))
        self.remaining = 8192
        self.limited = False
        self._rows = ([0] * 67, [0] * 67, [0] * 67)

    def _near(self, query: str, candidate: str) -> bool:
        """Uses banded Damerau-Levenshtein with reusable rows and a two-edit ceiling."""
        length, size = len(query), len(candidate)
        limit = 1 if length < 9 else 2
        if length < 4 or length > 64 or size > 66 or abs(length - size) > limit:
            return False
        if self.remaining == 0:
            self.limited = True
            return False
        self.remaining -= 1
        older, previous, current = self._rows
        ceiling = limit + 1
        for column in range(length + 1):
            older[column] = current[column] = ceiling
            previous[column] = column
        for row in range(1, size + 1):
            current[0] = row
            first, last = max(1, row - limit), min(length, row + limit)
            if first > 1:
                current[first - 1] = ceiling
            if last < length:
                current[last + 1] = ceiling
            best = ceiling
            for column in range(first, last + 1):
                cost = min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1]
                    + (query[column - 1] != candidate[row - 1]),
                )
                if (
                    row > 1
                    and column > 1
                    and query[column - 1] == candidate[row - 2]
                    and query[column - 2] == candidate[row - 1]
                ):
                    cost = min(cost, older[column - 2] + 1)
                current[column] = cost
                best = min(best, cost)
            if best > limit:
                return False
            older, previous, current = previous, current, older
        return previous[length] <= limit

    def matches(self, value: str) -> bool:
        """Keeps exact matching available after the approximate-comparison budget expires."""
        text = normalize(value)
        if not self.terms:
            return self.literal in text
        missing = [term for term in self.terms if term not in text]
        if not missing:
            return True
        if len(missing) > 16 or any(
            len(term) < 4 or len(term) > 64 for term in missing
        ):
            return False
        for index, match in enumerate(_WORDS.finditer(text)):
            if index >= 4096 or self.remaining == 0:
                self.limited = True
                return False
            word = match.group(0)
            missing = [term for term in missing if not self._near(term, word)]
            if not missing:
                return True
        return False
