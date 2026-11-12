.DEFAULT_GOAL := check

PYTHON ?= python3
COLLECTOR ?= tickets

export PYTHON COLLECTOR EXPORT ITEM WINDOW_START WINDOW_END SOURCE

.PHONY: check collect rank report

check:
	@"$$PYTHON" tools/validate_registry.py --strict
	@"$$PYTHON" -m pytest
	@"$$PYTHON" -m ruff check .
	@"$$PYTHON" -m ruff format --check .
	@"$$PYTHON" -m mypy automation collectors ranking tools

# Preview measurements from a local, already filtered export.
collect:
	@case "$$COLLECTOR" in tickets|alerts) ;; \
	  *) printf '%s\n' 'COLLECTOR must be tickets or alerts' >&2; exit 2 ;; esac; \
	: "$${EXPORT:?Set EXPORT to a local export path}" \
	  "$${ITEM:?Set ITEM to the registry item ID}" \
	  "$${WINDOW_START:?Set WINDOW_START to the first date}" \
	  "$${WINDOW_END:?Set WINDOW_END to the last date}" \
	  "$${SOURCE:?Set SOURCE to the export provenance}"; \
	exec "$$PYTHON" -m "collectors.$$COLLECTOR" \
	  --export "$$EXPORT" --id "$$ITEM" --from "$$WINDOW_START" \
	  --to "$$WINDOW_END" --source "$$SOURCE"

rank:
	@"$$PYTHON" -m ranking.score

report:
	@"$$PYTHON" -m ranking.score --json
