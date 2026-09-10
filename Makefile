# Works with GNU make (Git Bash / WSL on Windows). All targets use the venv.
PY := .venv/Scripts/python.exe
ifeq ($(OS),)
PY := .venv/bin/python
endif

.PHONY: install install-kokoro test test-unit lint typecheck check run-check callback-shim clean

install:
	python -m venv .venv || true
	$(PY) -m pip install -e ".[dev]"

install-kokoro:
	$(PY) -m pip install -e ".[dev,kokoro]"

test:
	$(PY) -m pytest tests

test-unit:
	$(PY) -m pytest tests -m "not integration"

typecheck:
	$(PY) -m mypy

check: typecheck test

run-check:
	$(PY) -m demo_narrator.cli config-check

clean:
	$(PY) -c "import shutil; shutil.rmtree('output', ignore_errors=True)"

callback-shim:
	$(PY) scripts/oauth_callback_shim.py --host 127.0.0.1 --port 4200 --target http://localhost:4201
