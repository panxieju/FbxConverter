"""Unreal package-path conversion tests (spec section 3)."""

from __future__ import annotations

import pytest

from fbxconv.unreal.paths import (
    is_under,
    normalise_mount,
    object_to_package,
    package_to_object,
    package_to_relative,
    sanitise_filename,
    to_filesystem,
    to_package_path,
    unique_posix,
)


class TestMount:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("Game", "/Game"), ("/Game/", "/Game"), ("/Game", "/Game"), ("", "/Game")],
    )
    def test_normalise(self, raw: str, expected: str):
        assert normalise_mount(raw) == expected


class TestPackageConversion:
    def test_to_package_path_strips_extension(self):
        assert to_package_path("A/B/SK_X.uasset") == "/Game/A/B/SK_X"

    def test_object_round_trip(self):
        package = "/Game/A/B/SK_X"
        assert package_to_object(package) == "/Game/A/B/SK_X.SK_X"
        assert object_to_package("/Game/A/B/SK_X.SK_X") == package

    def test_object_to_package_tolerates_bare_path(self):
        assert object_to_package("/Game/A/B") == "/Game/A/B"

    @pytest.mark.parametrize(
        ("package", "expected"),
        [
            ("/Game/A/B", "A/B"),
            ("/Game", "."),
            ("/Other/A", "Other/A"),
        ],
    )
    def test_package_to_relative(self, package: str, expected: str):
        assert str(package_to_relative(package)) == expected

    def test_is_under(self):
        assert is_under("/Game/A", "/Game")
        assert is_under("/Game", "/Game")
        assert not is_under("/Gameplay/A", "/Game")

    def test_to_filesystem(self, tmp_path):
        result = to_filesystem("/Game/A/SK_X", tmp_path)
        assert result.name == "SK_X.uasset"
        assert result.parent.name == "A"


class TestSanitise:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("SK_Mannequin", "SK_Mannequin"),
            ("a/b", "a_b"),
            ("a:b*c?", "a_b_c_"),
            ("  trailing  ", "trailing"),
            ("", "Unnamed"),
        ],
    )
    def test_sanitise_filename(self, raw: str, expected: str):
        assert sanitise_filename(raw) == expected


class TestUniquePosix:
    def test_dedupes_with_suffix(self):
        taken: set[str] = set()
        assert unique_posix("A.fbx", taken) == "A.fbx"
        assert unique_posix("A.fbx", taken) == "A_1.fbx"
        assert unique_posix("A.fbx", taken) == "A_2.fbx"

    def test_handles_missing_extension(self):
        taken: set[str] = set()
        assert unique_posix("A", taken) == "A"
        assert unique_posix("A", taken) == "A_1"
