# Licensing

| Path | License |
|---|---|
| `agent/`, `api/`, `ui/`, `data/`, `docs/`, other tests | Apache-2.0 (`LICENSE`) |
| `product/`, `tests/test_product.py`, `Dockerfile.product` | Commercial (`product/LICENSE`) |
| Root config (`Dockerfile`, `docker-compose*.yml`, `Makefile`, `requirements.txt`, `.env.example`, `pytest.ini`) | Apache-2.0 |

The open core is complete on its own: single shared `AGENT_API_KEY`, no tenants, no metering.
`product/LICENSE` is a complete draft (v0.1) whose blanks are `{{PLACEHOLDERS}}`; counsel must finalize it before publishing (see its Status section).
The commercial layer adds per-tenant API keys, plan limits, usage metering and Stripe usage reporting.
Releases already published under Apache-2.0 stay Apache-2.0; the split applies to `product/` going forward.
Contributions to the core are accepted under Apache-2.0 (no CLA). Contributions to `product/` require
the signed CLA in [`CLA.md`](CLA.md); the `CLA gate for product/` workflow blocks PRs that touch
`product/`, `tests/test_product.py` or `Dockerfile.product` from authors not listed in
`.github/CLA-SIGNATORIES.txt`.

## Legal documents for the hosted service

`product/legal_docs/` holds drafts of the Terms of Service, Privacy Policy, Data Processing Addendum and Refunds and
Cancellation text. The product serves them at `/terms`, `/privacy`, `/dpa` and `/refunds`, links them from every page, and
requires `accept_terms` at signup (the accepted version and time are stored on the tenant).

All blanks, in these texts and in `product/LICENSE` and `CLA.md`, come from one file, `product/legal_docs/entity.json`
(legal name, entity type, address, contact emails, governing law, venue, liability cap, refund window, hosting provider,
effective date). Fill it in, have counsel review every text, set `COUNSEL_REVIEWED` to `true`, then:

```
make legal-fill    # writes the values into product/LICENSE and CLA.md
make legal-check   # exit 0 only when nothing is blank and counsel has signed off
```

Until `legal-check` passes the legal pages answer 503 and public signup stays closed, so an unfinished draft is never shown
to customers. Bump `VERSION` in `product/legal.py` whenever the text changes. The drafts describe what the code does today
(what is stored, what is sent to the AI provider, what is deleted when); if you change that, change the text.
