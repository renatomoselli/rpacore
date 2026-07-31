"""Automation-definition identity validation."""

from __future__ import annotations

import unicodedata

from rpacore.exceptions import DefinitionIdentityError


_MAX_DEFINITION_IDENTITY_LENGTH = 255


def validate_definition_identity(
    value: object,
    *,
    field: str,
    required: bool = False,
) -> str:
    """Return a portable opaque identity or raise a permanent validation error."""
    if value is None and required:
        raise DefinitionIdentityError(f"{field} must be a non-empty str")
    if not isinstance(value, str):
        raise DefinitionIdentityError(
            f"{field} must be a str, got {type(value).__name__}"
        )
    if not value:
        if required:
            raise DefinitionIdentityError(f"{field} must be a non-empty str")
        return value
    if value != value.strip():
        raise DefinitionIdentityError(
            f"{field} must not have leading or trailing whitespace"
        )
    if len(value) > _MAX_DEFINITION_IDENTITY_LENGTH:
        raise DefinitionIdentityError(
            f"{field} must be at most {_MAX_DEFINITION_IDENTITY_LENGTH} characters"
        )
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise DefinitionIdentityError(f"{field} must not contain control characters")
    return value
