"""Unity-side integration: manifest/profile generation and the C# importer."""

from __future__ import annotations

from .manifest import (
    IMPORTER_FILENAME,
    build_manifest_payload,
    build_profile_payload,
    find_enclosing_project,
    infer_loop,
    install_unity_support,
    project_has_test_framework,
    write_json,
)
from .runner import UnityInstall, find_unity_editors, run_batch_import

__all__ = [
    "IMPORTER_FILENAME",
    "UnityInstall",
    "build_manifest_payload",
    "build_profile_payload",
    "find_enclosing_project",
    "find_unity_editors",
    "infer_loop",
    "install_unity_support",
    "project_has_test_framework",
    "run_batch_import",
    "write_json",
]
