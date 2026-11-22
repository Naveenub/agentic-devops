# Licensing

| Path | License |
|---|---|
| `agent/`, `api/`, `ui/`, `data/`, `docs/`, other tests | Apache-2.0 (`LICENSE`) |
| `product/`, `tests/test_product.py`, `Dockerfile.product` | Commercial (`product/LICENSE`) |

The open core is complete on its own: single shared `AGENT_API_KEY`, no tenants, no metering.
The commercial layer adds per-tenant API keys, plan limits, usage metering and Stripe usage reporting.
Releases already published under Apache-2.0 stay Apache-2.0; the split applies to `product/` going forward.
Contributions to the core are accepted under Apache-2.0. Before accepting outside contributions to
`product/`, put a contributor agreement in place.
