.PHONY: check lint type test fmt

check: lint type test

lint:
	ruff check .
	ruff format --check .

type:
	mypy

test:
	pytest --cov=mobile_factory --cov-report=term-missing:skip-covered

fmt:
	ruff check --fix .
	ruff format .
