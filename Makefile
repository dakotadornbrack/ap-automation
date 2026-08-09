.PHONY: help install db data pipeline rules report dashboard test lint clean

DATABASE_URL ?= postgresql://ap:ap@localhost:5432/ap
export DATABASE_URL
export PYTHONPATH := src

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## install dev dependencies
	pip install -r requirements-dev.txt

db:  ## start postgres
	docker compose up -d db

data:  ## generate the synthetic sample CSV
	python -m ap.generate --count 3000 --out data/raw/invoices_sample.csv

pipeline: data  ## migrate, ingest, run rules, print report
	python -m ap.cli pipeline data/raw/invoices_sample.csv --force

rules:  ## re-run the rule engine only
	python -m ap.cli rules

report:  ## print the console summary
	python -m ap.cli report

dashboard:  ## launch the dashboard
	streamlit run dashboard/app.py

test:  ## run the test suite with coverage
	pytest --cov=ap --cov-report=term-missing --cov-report=xml

lint:  ## lint
	ruff check src tests dashboard

clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name __pycache__ -type d -exec rm -rf {} +
