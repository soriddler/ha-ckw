#!/bin/sh
# Library tests (py3.10 venv, aioresponses) and Home Assistant tests (py3.14 venv, HA 2026.11)
cd "$(dirname "$0")/.." || exit 1
.venv/bin/ruff check custom_components tests tools --select F,E9 -q && .venv/bin/pytest -q "$@" && .venv-ha/bin/pytest -q -o addopts="" -p no:cacheprovider tests/ha "$@"
