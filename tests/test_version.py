"""The version number lives in app/__init__.py; pyproject.toml must agree."""

import tomllib
from pathlib import Path

from app import __version__
from app.models import HealthResponse


def test_pyproject_version_matches_package():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


def test_health_reports_package_version():
    assert HealthResponse().version == __version__
