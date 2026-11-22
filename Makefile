.PHONY: legal-check legal-fill help ingest eval live-eval test report report-check up image image-product stripe-check stripe-sync billing-report

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

# Stripe vs product/plans.py: check exits 1 on drift; sync creates what is missing and prints the env to set
stripe-check:
	python -m product.stripe_sync

stripe-sync:
	python -m product.stripe_sync --apply

# One Stripe usage report now (needs PRODUCT_DB and STRIPE_*). Not needed when PRODUCT_BILLING_INTERVAL is set;
# use it from cron / a Kubernetes CronJob if you would rather schedule it outside the API. Exit 1 if a batch failed.
billing-report:
	python -m product.billing

# Open-core API on http://localhost:8000
up:
	docker compose up --build

image:
	docker build -t agentic-devops-core .

# Commercial image (adds product/, see LICENSING.md). Builds the core image first.
image-product: image
	docker build -f Dockerfile.product -t agentic-devops-product .

# Legal readiness (see LICENSING.md): exit 1 while any entity field is blank, counsel has not signed off, or
# product/LICENSE / CLA.md still hold {{PLACEHOLDERS}}. Run before publishing the repo or opening signup.
legal-check:
	python -m product.legal check

# Write the values from product/legal_docs/entity.json into product/LICENSE and CLA.md
legal-fill:
	python -m product.legal fill
