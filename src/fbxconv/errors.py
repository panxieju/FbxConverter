"""Exception hierarchy for the conversion pipeline.

Every failure that a user could act on carries a machine-readable ``code``
and, where relevant, the concrete paths involved, so the UI and the export
report can both explain *what* is wrong and *where*.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class FbxConvError(Exception):
    """Base class for all expected pipeline failures."""

    code = "fbxconv-error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.detail:
            return f"{self.message}\n{self.detail}"
        return self.message


class ConfigError(FbxConvError):
    """The user's configuration is incomplete or contradictory."""

    code = "config-error"


class UnrealNotFoundError(FbxConvError):
    """No usable Unreal Editor installation could be located."""

    code = "unreal-not-found"

    def __init__(self, message: str, *, searched: Sequence[str] = (), detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.searched = tuple(searched)


class UnrealRunError(FbxConvError):
    """The Unreal Editor process failed, crashed, or returned no result."""

    code = "unreal-run-failed"

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        log_path: Path | None = None,
        output_tail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.log_path = log_path
        self.output_tail = output_tail

    def __str__(self) -> str:
        parts = [self.message]
        if self.returncode is not None:
            parts.append(f"exit code: {self.returncode}")
        if self.log_path is not None:
            parts.append(f"log: {self.log_path}")
        if self.output_tail:
            parts.append("--- last output ---\n" + self.output_tail)
        return "\n".join(parts)


class UnrealScriptError(FbxConvError):
    """The Unreal-side Python script reported a structured failure."""

    code = "unreal-script-failed"

    def __init__(self, message: str, *, traceback_text: str | None = None) -> None:
        super().__init__(message)
        self.traceback_text = traceback_text

    def __str__(self) -> str:
        if self.traceback_text:
            return f"{self.message}\n{self.traceback_text}"
        return self.message


class DiscoveryError(FbxConvError):
    """The resource directory could not be interpreted."""

    code = "discovery-failed"


class ContentRootError(DiscoveryError):
    """The chosen directory is not a usable Unreal content root."""

    code = "content-root-invalid"


class ScanError(FbxConvError):
    """Asset registry scanning failed."""

    code = "scan-failed"


class ExportError(FbxConvError):
    """A conversion run failed as a whole (individual item failures are reported per item)."""

    code = "export-failed"


class CancelledError(FbxConvError):
    """The user cancelled the running operation."""

    code = "cancelled"


__all__ = [
    "CancelledError",
    "ConfigError",
    "ContentRootError",
    "DiscoveryError",
    "ExportError",
    "FbxConvError",
    "ScanError",
    "UnrealNotFoundError",
    "UnrealRunError",
    "UnrealScriptError",
]
