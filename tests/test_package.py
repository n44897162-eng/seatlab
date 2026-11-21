"""Packaging sanity: the installed distribution exposes what the docs promise."""

from __future__ import annotations

from importlib import resources
from importlib.metadata import PackageNotFoundError, version

import pytest

import seatlab


def test_version_is_consistent() -> None:
    assert seatlab.__version__ == "1.4.0"
    try:
        installed = version("seatlab")
    except PackageNotFoundError:  # running from a source checkout, not installed
        pytest.skip("seatlab is not installed as a distribution")
    assert installed == seatlab.__version__


def test_static_pages_ship_with_the_package() -> None:
    static = resources.files("seatlab") / "static"
    assert (static / "index.html").is_file()
    lab = static / "lab.html"
    assert lab.is_file() and lab.stat().st_size > 80_000
    for page in ("design.html", "admin.html", "app.html"):
        asset = static / page
        assert asset.is_file() and asset.stat().st_size > 10_000


def test_typing_marker_exists() -> None:
    assert (resources.files("seatlab") / "py.typed").is_file()


def test_public_api_is_importable() -> None:
    for name in ("Params", "Policy", "run", "compare", "optimize", "simulate_day", "erlang_c"):
        assert hasattr(seatlab, name), name
