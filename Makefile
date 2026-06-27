PY := ./.venv/bin/python

.PHONY: install demo test serve stacks mcp-add clean

install:        ## One-time setup (venv + deps + config)
	./bootstrap.sh

demo:           ## Run the scripted end-to-end showcase (mock mode)
	$(PY) -m terrapilot.cli demo

test:           ## Run the end-to-end test suite
	$(PY) -m pytest -q

serve:          ## Run the MCP server over stdio
	$(PY) -m terrapilot.server

stacks:         ## List discoverable stacks
	$(PY) -m terrapilot.cli stacks --limit 20

mcp-add:        ## Register the server with Claude Code
	claude mcp add terrapilot -- $(abspath $(PY)) -m terrapilot.server

clean:          ## Remove runtime state and caches
	rm -rf .terrapilot .pytest_cache **/__pycache__

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-12s\033[0m %s\n",$$1,$$2}'
