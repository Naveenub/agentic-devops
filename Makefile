.PHONY: help ingest eval live-eval test report report-check up image image-product

help:
	@echo "Targets: test eval live-eval report report-check ingest up image image-product"

ingest:
	python -m agent.memory.ingest

eval:
	python -m agent.eval
	python -m agent.hallucination_eval

test:
	python -m pytest -q

live-eval:
	python -m agent.eval --live
	python -m agent.hallucination_eval --judge

report:
	python -m agent.report

report-check:
	python -m agent.report --check

# Open-core API on http://localhost:8000
up:
	docker compose up --build

image:
	docker build -t agentic-devops-core .

# Commercial image (adds product/, see LICENSING.md). Builds the core image first.
image-product: image
	docker build -f Dockerfile.product -t agentic-devops-product .
