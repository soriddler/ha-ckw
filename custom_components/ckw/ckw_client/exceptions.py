"""Exceptions raised by the CKW client."""
from __future__ import annotations


class CkwError(Exception):
    """Base class."""


class CannotConnect(CkwError):
    """Network problem, timeout, or the service answered with a 5xx / unexpected status."""


class InvalidAuth(CkwError):
    """Username or password rejected."""


class InvalidCode(CkwError):
    """SMS code rejected. The login session is still usable: try again with the newest code."""


class ReauthRequired(CkwError):
    """The stored refresh token is no longer valid (invalid_grant): a new SMS login is needed."""


class LoginFlowError(CkwError):
    """The login pages did not look as expected (portal changed, bot protection, expired session)."""


class ParseError(CkwError):
    """A data file or response did not have the expected structure."""
