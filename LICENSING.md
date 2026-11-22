# Licensing

| Path | License |
|---|---|
| `agent/`, `api/`, `ui/`, `data/`, `docs/`, other tests | Apache-2.0 (`LICENSE`) |
| `product/`, `tests/test_product.py`, `Dockerfile.product` | Commercial (`product/LICENSE`) |
| Root config (`Dockerfile`, `docker-compose*.yml`, `Makefile`, `requirements.txt`, `.env.example`, `pytest.ini`) | Apache-2.0 |

The open core is complete on its own: single shared `AGENT_API_KEY`, no tenants, no metering.
`product/LICENSE` is a complete draft (v0.1) with bracketed fields; counsel must finalize it before publishing (see its Status section).
The commercial layer adds per-tenant API keys, plan limits, usage metering and Stripe usage reporting.
Releases already published under Apache-2.0 stay Apache-2.0; the split applies to `product/` going forward.
Contributions to the core are accepted under Apache-2.0 (no CLA). Contributions to `product/` require
the signed CLA in [`CLA.md`](CLA.md); the `CLA gate for product/` workflow blocks PRs that touch
`product/`, `tests/test_product.py` or `Dockerfile.product` from authors not listed in
`.github/CLA-SIGNATORIES.txt`.
