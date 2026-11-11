PROJECT_ROOT := $(shell dirname "$(CURDIR)")
PYTHON ?= $(PROJECT_ROOT)/.venv/bin/python
NODE ?= node
PORT ?= 8100

.PHONY: setup run check build build-frontend test stress walkthrough release
setup:
	cd "$(PROJECT_ROOT)" && python3 -m venv .venv
	"$(PROJECT_ROOT)/.venv/bin/python" -m pip install -r "$(PROJECT_ROOT)/backend/requirements.txt"
run:
	cd "$(PROJECT_ROOT)" && INVOICECLEAR_PORT=$(PORT) "$(PYTHON)" backend/run.py
build:
	cd "$(PROJECT_ROOT)" && "$(NODE)" frontend/scripts/build_frontend.mjs
build-frontend: build
test:
	cd "$(PROJECT_ROOT)/backend" && "$(PYTHON)" -m unittest discover -s tests -v
stress:
	cd "$(PROJECT_ROOT)" && "$(PYTHON)" backend/scripts/stress_release.py
walkthrough:
	cd "$(PROJECT_ROOT)" && "$(PYTHON)" backend/scripts/walkthrough_release.py
release: check walkthrough stress
check:
	cd "$(PROJECT_ROOT)" && "$(PYTHON)" -m compileall -q backend
	cd "$(PROJECT_ROOT)" && "$(NODE)" --check frontend/web/app.js
	cd "$(PROJECT_ROOT)" && "$(NODE)" --check frontend/web/interactions.js
	cd "$(PROJECT_ROOT)" && "$(NODE)" --check frontend/scripts/build_frontend.mjs
	cd "$(PROJECT_ROOT)" && "$(NODE)" frontend/scripts/test_build.mjs
	cd "$(PROJECT_ROOT)" && "$(NODE)" frontend/scripts/test_workflow.mjs
	cd "$(PROJECT_ROOT)" && "$(NODE)" frontend/scripts/test_pagination.mjs
	cd "$(PROJECT_ROOT)" && "$(NODE)" frontend/scripts/test_interactions.mjs
	$(MAKE) build
	$(MAKE) test
