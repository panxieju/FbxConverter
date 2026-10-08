"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ue4_mannequin_tree(tmp_path: Path) -> Path:
    """A miniature Unreal content tree mimicking a UE4 character pack."""
    content = tmp_path / "Content"
    meshes = content / "Mannequin_UE4" / "Meshes"
    animations = content / "Mannequin_UE4" / "Animations"
    meshes.mkdir(parents=True)
    animations.mkdir(parents=True)

    for name in ("SK_Mannequin.uasset", "SK_Mannequin_Skeleton.uasset",
                 "SK_Mannequin_PhysicsAsset.uasset"):
        (meshes / name).write_bytes(b"\x00" * 32)
    (meshes / "SK_Mannequin.uexp").write_bytes(b"\x00" * 8)
    (meshes / "SK_Mannequin.ubulk").write_bytes(b"\x00" * 8)
    (animations / "Jog_Fwd.uasset").write_bytes(b"\x00" * 16)
    return content


@pytest.fixture
def content_root_with_project(tmp_path: Path) -> Path:
    """A content root that belongs to a real ``.uproject``."""
    project = tmp_path / "MyGame"
    content = project / "Content"
    (content / "Characters").mkdir(parents=True)
    (project / "MyGame.uproject").write_text('{"FileVersion": 3}', encoding="utf-8")
    (content / "Characters" / "SK_Hero.uasset").write_bytes(b"\x00" * 16)
    return content
