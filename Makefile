.PHONY: prod-up prod-down prod-logs prod-backup mail-test accuracy-claim legal-check legal-fill help ingest eval live-eval test report report-check up image image-product stripe-check stripe-sync stripe-e2e billing-report pricing-check pricing-measure

help:
	@echo "Targets: test eval live-eval report report-check ingest up image image-product"

ingest:
	python -m agent.memory.ingest

eval:
	python -m agent.eval
	python -m agent.hallucination_eval

test:
	python -m pytest -q

# Smoke run of the live model on the hand-written set. Not an accuracy result (0 real cases is allowed here).
live-eval:
	python -m agent.eval --live --allow-golden-only
	python -m agent.hallucination_eval --judge

# The only target whose pass means something: live model, real incidents required (>= 20), per-claim judge.
accuracy-claim:
	python -m agent.eval --live --min-real 20
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

# One real Stripe TEST-mode run: sync, checkout, webhook, usage report, cancel. Needs STRIPE_SECRET_KEY=sk_test_... (live keys refused).
stripe-e2e:
	python -m product.stripe_e2e

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

# Pricing readiness (product/pricing_evidence.json): exit 1 while cost inputs are missing, a plan's worst-case margin is
# below MIN_GROSS_MARGIN, there is no willingness-to-pay evidence, or PRICES_CONFIRMED is not true. Run before `stripe-sync --apply`.
pricing-check:
	python -m product.unit_economics check

# Measure real tokens per analysis (needs OPENAI_API_KEY / OPENAI_BASE_URL) and record them in pricing_evidence.json
pricing-measure:
	python -m product.unit_economics measure

# Hosted deployment (docker-compose.prod.yml: Caddy HTTPS + api + PostgreSQL). Needs .env.production.
PROD = docker compose --env-file .env.production -f docker-compose.prod.yml

prod-up: image-product
	$(PROD) up -d

prod-down:
	$(PROD) down

prod-logs:
	$(PROD) logs -f --tail=100 api caddy

prod-backup:
	@mkdir -p backups
	$(PROD) exec -T db pg_dump -U agent agent | gzip > backups/agent-$$(date +%F-%H%M).sql.gz

# Send a real test email through the configured SMTP server: make mail-test TO=you@example.com
mail-test:
	@test -n "$(TO)" || { echo "usage: make mail-test TO=you@example.com"; exit 2; }
	$(PROD) exec -T api python -c "from product import mailer; assert mailer.configured(), 'SMTP_HOST/SMTP_FROM/PUBLIC_URL not all set'; mailer.send('$(TO)', 'Mail test', 'SMTP works.'); print('sent')"
