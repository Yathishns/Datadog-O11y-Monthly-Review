# Acme Retail — Observability Review, November 2026

**What Acme Retail's Shoppers — web and app, Payments and Customer communications systems did in the week to 19 Nov 2026, what it means for shoppers and finance, and what to do next.**

Prepared for Acme Retail Digital · Vsceptre Observability-as-a-Service · 19 Nov 2026

> 📖 **How to read this notebook.** Eight findings, each with the evidence, the impact and the actions. Graphs marked *➕ live graph* can be added in a minute with `/timeseries` so the page stays current. Numbers come from Datadog APM's Endpoints list (the week to 19 Nov 2026), the APM Services list and a trace sample of 9 minutes (19 Nov 02:00–02:09 UTC). Use **⋮ → Download as PDF** to circulate.

---

## 📌 Executive summary

| # | Finding | Who feels it | Severity | Action | Trend |
|---|---|---|---|---|---|
| 1 | **Production calls a UAT endpoint** — `uat-pricing.acme.internal` (603,000 calls in the Services-list export, about 6 days) | Everyone | 🔴 | Remove the non-production references | 🆕 new |
| 2 | **One in twelve card authorisations fails** on `payment-gateway` (31,274 of 347,492 in the week to 19 Nov 2026); refunds fail 33.0 %; clients request about 2.92 new tokens per business call | Shoppers and finance | 🔴 | Root-cause the `POST /pay/authorise` failures; alert on regressions; cache tokens until expiry | → flat |
| 3 | **One in twenty checkouts fails** on `checkout-api` (18,411 of 409,143 in the week to 19 Nov 2026) | Shoppers | 🔴 | Root-cause the `POST /api/checkout` failures; alert on regressions | ▲ worse |
| 4 | **One dependency is failing** — `legacy-erp.acme.internal` fails 100 % (1,440 of 1,440 calls in the Services-list export, about 6 days, p95 3.10 s) | Users and staff, depending on the caller | 🔴 | Assign an owner; fix or retire | ▲ worse |
| 5 | **Payment gateway is slow on two of its features** — 26 % of its traffic is on operations with p95 above 2.00 s; the slowest frequent one is `POST /pay/authorise` at 2.66 s | Shoppers and finance | 🟠 | Profile the request path; set a latency target | → flat |
| 6 | **One slow operation in Shoppers — web and app** — `GET /api/search` takes 4.39 s at p95 on 895,941 calls in the week to 19 Nov 2026 | Shoppers | 🟠 | Tune the slow queries and downstream calls | → flat |
| 7 | **1.20 % of `send_email` calls fail** on `notify-worker` (3,076 of 256,313 in the week to 19 Nov 2026); it has **no monitors** | Shoppers | 🟠 | Root-cause the `send_email` failures; alert on regressions | → flat |
| 8 | **Observability foundations are thin** — Monitors on 2 of 4 services; no SLOs; scanner traffic in APM | Everyone who operates the estate | 🟠 | Monitors on every service, SLOs and ingestion hygiene | → flat |

**Delivered alongside this review:** the *Acme [Leadership] — Shop Health at a Glance* dashboard and a pack of 6 monitors

---

## 📈 Since last month (October 2026)

One is new, seven persist, one escalated in severity and one persists but moved to *Also noted*.

| Finding | Metric | October 2026 | November 2026 | Trend | Status |
|---|---|---|---|---|---|
| One in twelve card authorisations fails on Payment gateway; refunds fail 33.0 % | failure rate of `POST /pay/authorise` | 9.00 % | 9.00 % | → flat | persisting |
| One in twenty checkouts fails on Checkout API | failure rate of `POST /api/checkout` | 2.10 % | 4.50 % | ▲ worse | persisting |
| One dependency is failing | failure rate of `legacy-erp.acme.internal` | 2.01 % | 100 % | ▲ worse | persisting — now 🔴 (was 🟠) |
| Payment gateway is slow on two of its features | share of payment-gateway traffic with p95 above 2.00 s | 25.8 % | 25.5 % | → flat | persisting |
| One slow operation in Shoppers — web and app: `GET /api/search` (product searches) takes 4.39 s at p95 | p95 of `GET /api/search` | 4.39 s | 4.39 s | → flat | persisting |
| 1.20 % of `send_email` calls fail on Notifications, and nothing is watching it | failure rate of `send_email` | 1.20 % | 1.20 % | → flat | persisting |
| Observability foundations are thin | foundation gaps | 3 | 3 | → flat | persisting |
| Vulnerability scans reach Checkout API | scanner probe requests on `checkout-api` | 141 | 98 | ▼ better | persisting — now in Also noted |
| 🆕 Production calls a UAT endpoint | calls to `uat-pricing.acme.internal` | — | 603,000 | — | new |

**What moved** (beyond the tolerances in the config):

| Where | Metric | October 2026 | November 2026 | Change |
|---|---|---|---|---|
| `checkout-api` | failure rate | 0.68 % | 1.30 % | +0.62 pt |
| checkouts (operation) | failure rate | 2.10 % | 4.50 % | +2.40 pt |
| refunds (operation) | failure rate | 45.0 % | 33.0 % | -12.00 pt |
| `legacy-erp.acme.internal` (dependency) | failure rate | 2.01 % | 100 % | +97.99 pt |

---

## 🗺️ What Datadog sees today

Four traced services, 15 endpoints and 8.2 million requests in the week to 19 Nov 2026, across three domains. The map shows only connections the data shows (parent/child spans).

```mermaid
flowchart TB
  subgraph B_shop["🛒 Shoppers — web and app"]
    direction LR
    S_catalog_api["Catalog API<br/>Java servlet · 4.7 M req"]
    S_checkout_api["Checkout API<br/>Java servlet · 1.8 M req"]
    D_uat_pricing_acme_internal(["uat-pricing.acme.internal<br/>UAT host"])
    D_postgres[("postgres")]
    D_redis[("redis")]
  end
  subgraph B_payments["💳 Payments"]
    direction LR
    S_payment_gateway["Payment gateway<br/>Java servlet · 1.5 M req"]
    D_api_stripe_com(["api.stripe.com<br/>8.10 % fail"])
  end
  subgraph B_comms["✉️ Customer communications"]
    direction LR
    S_notify_worker["Notifications<br/>Java servlet · 256 k req"]
  end
  D_legacy_erp_acme_internal(["legacy-erp.acme.internal<br/>100 % fail · p95 3.10 s"])
  S_catalog_api --> D_uat_pricing_acme_internal
  S_catalog_api --> D_postgres
  S_checkout_api --> D_postgres
  S_payment_gateway --> D_postgres
  S_catalog_api --> D_redis
  S_checkout_api --> D_redis
  S_payment_gateway --> D_api_stripe_com
  S_checkout_api --> D_api_stripe_com
  style S_catalog_api fill:#fff3cd,stroke:#b8860b,color:#000
  style S_checkout_api fill:#ffe0e0,stroke:#c62828,color:#000
  style S_payment_gateway fill:#ffe0e0,stroke:#c62828,color:#000
  style S_notify_worker fill:#fff3cd,stroke:#b8860b,color:#000
  style D_uat_pricing_acme_internal fill:#ffe0e0,stroke:#c62828,color:#000
  style D_legacy_erp_acme_internal fill:#ffe0e0,stroke:#c62828,color:#000
```

| Domain | Services | Requests (week to 19 Nov 2026) | Failed (business) | p95 |
|---|---|---|---|---|
| 🛒 Shoppers — web and app | `catalog-api`, `checkout-api` | 6,495,704 (6,092,406 business) | 0.43 % (0.41 % counting health checks and other noise) | **1.67 s–4.39 s** |
| 💳 Payments | `payment-gateway` | 1,483,968 | 2.80 % | **2.66 s** |
| ✉️ Customer communications | `notify-worker` | 256,313 | 1.20 % | 746 ms |

```mermaid
pie showData
  title Requests by domain, week to 19 Nov 2026
  "Shoppers — web and app" : 6495704
  "Payments" : 1483968
  "Customer communications" : 256313
```

> 🎨 **A rule used throughout:** a request is *failed* when it returns a 5xx or records an application error; 4xx responses are not failures. Volumes count every request (APM trace metrics), not a sample.

---

## 🔴 Finding 1 — Production calls a UAT endpoint

A production service that calls a test or UAT host is a configuration error waiting to become an incident: the host can be rebuilt, reset or switched off without notice, and its data is not production data. `uat-pricing.acme.internal` was called 603,000 times from `catalog-api` in the Services-list export, about 6 days (p95 250 ms).

| Reference | Calls | p95 | Called from |
|---|---|---|---|
| `uat-pricing.acme.internal` — a **UAT** host | 603,000 | 250 ms | `catalog-api` |

*Dependency map for Findings 1 and 4 (the hosts those findings name):*

```mermaid
flowchart LR
  D_uat_pricing_acme_internal(["uat-pricing.acme.internal"])
  S_catalog_api["catalog-api"]
  D_legacy_erp_acme_internal(["legacy-erp.acme.internal"])
  P_production_unknown["Production services (caller unknown)"]
  S_catalog_api -->|"production → UAT"| D_uat_pricing_acme_internal
  P_production_unknown -->|"fails every call"| D_legacy_erp_acme_internal
  style D_uat_pricing_acme_internal fill:#ffe0e0,stroke:#c62828,color:#000
  style D_legacy_erp_acme_internal fill:#ffe0e0,stroke:#c62828,color:#000
```

**Actions**
1. Remove or repoint the `uat-pricing.acme.internal` reference in production.

> 🔎 open APM › Software Catalog › `uat-pricing.acme.internal` › Dependencies for the callers and the live error rate (peer-tag metrics are not exposed on trace.* metrics)

---

## 🔴 Finding 2 — One in twelve card authorisations fails on Payment gateway; refunds fail 33.0 %

`payment-gateway` (part of Payments: card authorisation and refunds through the payment provider) handled 1.5 million business requests in the week to 19 Nov 2026 and failed 41,528 of 1,483,968 (2.80 %). They concentrate on two operations:

| Operation | Calls | Failed | Failure rate | p95 |
|---|---|---|---|---|
| `POST /pay/authorise` — card authorisations | 347,492 | **31,274** | **9.00 %** | 2.66 s |
| `POST /pay/refund` — refunds | 31,072 | **10,254** | **33.0 %** | 2.49 s |

- **`POST /pay/authorise` fails 31,274 of 347,492 (9.00 %)** in the week to 19 Nov 2026 — about 4,468 a day. Each failure is a user or an operator who has to retry, or work that is not done.
- **`POST /pay/refund` fails 10,254 of 31,072 (33.0 %)** — fewer calls, but the highest failure rate on the service: one in three calls fails.
- The agreed target for card authorisations is 99.5 % success; in the week to 19 Nov 2026 it achieved 91.0 %.
- 1,105,404 calls to `POST /oauth/token` for 378,564 calls to `payment-gateway`'s other business operations in the week to 19 Nov 2026 is **2.92 per call**. Tokens are normally cached until they expire; requesting them per call multiplies the load on the token endpoint, and each token request takes up to 89 ms (p95) before the business call can start.

**Actions**
1. Pull one day of `POST /pay/authorise` failures from the application log and classify them (validation, data, timeout, dependency).
2. Alert when `POST /pay/authorise` fails more than 380 times in an hour (about 2.0× the hourly average in the week to 19 Nov 2026, which is 186), and lower the threshold as the fix lands.
3. Track card authorisations against the agreed 99.5 % success target as a Datadog SLO, so the burn rate pages before the target is missed.
4. Cache the token in each client of `payment-gateway` until shortly before it expires — that removes at least 726,840 calls in the week to 19 Nov 2026 (the excess over one token per business call) and up to nearly all 1,105,404.

> ➕ **live graph** — `/timeseries` · `sum:trace.servlet.request.hits{service:payment-gateway,resource_name:post_/pay/authorise}.as_count()` (requests); `/timeseries` · `sum:trace.servlet.request.errors{service:payment-gateway,resource_name:post_/pay/authorise}.as_count()` (failures, as red bars)

*Since last month: failure rate of `POST /pay/authorise`: 9.00 % → 9.00 % (→ flat).*

---

## 🔴 Finding 3 — One in twenty checkouts fails on Checkout API

`checkout-api` (part of Shoppers — web and app: the storefront's catalogue, cart and checkout) handled 1.6 million business requests in the week to 19 Nov 2026 and failed 20,800 of 1,603,503 (1.30 %). The failures concentrate on one operation:

| Operation | Calls | Failed | Failure rate | p95 |
|---|---|---|---|---|
| `POST /api/checkout` — checkouts | 409,143 | 18,411 | 4.50 % | 1.67 s |

- **`POST /api/checkout` fails 18,411 of 409,143 (4.50 %)** in the week to 19 Nov 2026 — about 2,630 a day. Each failure is a user or an operator who has to retry, or work that is not done.
- The agreed target for checkouts is 99.5 % success; in the week to 19 Nov 2026 it achieved 95.5 %.

**Actions**
1. Pull one day of `POST /api/checkout` failures from the application log and classify them (validation, data, timeout, dependency).
2. Alert when `POST /api/checkout` fails more than 220 times in an hour (about 2.0× the hourly average in the week to 19 Nov 2026, which is 110), and lower the threshold as the fix lands.
3. Track checkouts against the agreed 99.5 % success target as a Datadog SLO, so the burn rate pages before the target is missed.

> ➕ **live graph** — `/timeseries` · `sum:trace.servlet.request.hits{service:checkout-api,resource_name:post_/api/checkout}.as_count()` (requests); `/timeseries` · `sum:trace.servlet.request.errors{service:checkout-api,resource_name:post_/api/checkout}.as_count()` (failures, as red bars)

*Since last month: failure rate of `POST /api/checkout`: 2.10 % → 4.50 % (▲ worse).*

---

## 🔴 Finding 4 — One dependency is failing

Datadog infers these services from outbound calls. In the Services-list export, about 6 days:

| Dependency | Calls | Failed | p95 | Callers |
|---|---|---|---|---|
| `legacy-erp.acme.internal` — fails every time | 1,440 | **100 % (1,440)** | **3.10 s** | none in the sample |
| `api.stripe.com` | 520,000 | 8.10 % (42,120) | 520 ms | `payment-gateway`, `checkout-api` |

- `legacy-erp.acme.internal`: 1,440 of 1,440 calls failed in the Services-list export, about 6 days (p95 3.10 s across all its calls) — either decommissioned with something still calling, or down.
- Also failing, below the 15 % dependency bar: `api.stripe.com` 8.10 % (42,120 of 520,000 calls; callers `payment-gateway` and `checkout-api`).
- Callers come from the span sample (parent/child span join); the dependency's page in APM › Software Catalog shows the full picture.

*The dependency map in Finding 1 (shared by Findings 1 and 4) shows these hosts.*

**Actions**
1. Assign an owner to `legacy-erp.acme.internal` and agree a fix or a retirement date.
2. Retire the calls to `legacy-erp.acme.internal` or restore the service.

> 🔎 open APM › Software Catalog › `legacy-erp.acme.internal` › Dependencies for the callers and the live error rate (peer-tag metrics are not exposed on trace.* metrics)

*Since last month: failure rate of `legacy-erp.acme.internal`: 2.01 % → 100 % (▲ worse; severity 🟠 → 🔴).*

---

## 🟠 Finding 5 — Payment gateway is slow on two of its features

`payment-gateway` (part of Payments: card authorisation and refunds through the payment provider) handled 1.5 million business requests in the week to 19 Nov 2026 with a 2.80 % failure rate overall — but **26 % of that traffic sits on operations whose p95 is above 2.00 s**:

| Feature | Calls (week to 19 Nov 2026) | p95 |
|---|---|---|
| `POST /pay/authorise` — card authorisations | 347,492 | 2.66 s |
| `POST /pay/refund` — refunds | 31,072 | 2.49 s |

*What the traces show inside a request (in the 9-minute sample; ratios are sample ratios):*

```mermaid
flowchart LR
  R_request["Payment gateway request"]
  C_framework{"Java servlet"}
  K_http(["HTTP calls"])
  K_db[("database")]
  R_request --> C_framework
  C_framework -->|"~0.3 per request"| K_http
  C_framework -->|"~0.2 per request"| K_db
```

**Actions**
1. Turn on the Datadog continuous profiler for `payment-gateway` for a day — it shows wall time by function, including time the traces do not cover.
2. Set a latency target for the slow operations of `payment-gateway` (for example p95 under 2.00 s) and track it as an SLO.

> ➕ **live graph** — `/timeseries` · `p95:trace.servlet.request{service:payment-gateway}` (add a marker at the threshold); `/toplist` · `p95:trace.servlet.request{service:payment-gateway} by {resource_name}` (slowest features)

*Since last month: share of payment-gateway traffic with p95 above 2.00 s: 25.8 % → 25.5 % (→ flat).*

---

## 🟠 Finding 6 — One slow operation in Shoppers — web and app: `GET /api/search` (product searches) takes 4.39 s at p95

Most of what `catalog-api` serves is quick, but one frequent operation is slow in the week to 19 Nov 2026:

| Operation | Calls | p95 |
|---|---|---|
| `GET /api/search` — product searches | 895,941 | 4.39 s |

- `GET /api/search` is the costliest: 895,941 calls at 4.39 s p95 — each call keeps a user waiting.
- The agreed p95 target for product searches is 2.00 s; it runs at 4.39 s on `catalog-api`.

**Actions**
1. Profile `GET /api/search` on `catalog-api` (query plan, downstream calls) to bring it under its agreed 2.00 s p95.

> ➕ **live graph** — `/timeseries` · `p95:trace.servlet.request{service:catalog-api}` (add a marker at the threshold); `/toplist` · `p95:trace.servlet.request{service:catalog-api} by {resource_name}` (slowest features)

*Since last month: p95 of `GET /api/search`: 4.39 s → 4.39 s (→ flat).*

---

## 🟠 Finding 7 — 1.20 % of `send_email` calls fail on Notifications, and nothing is watching it

`notify-worker` (part of Customer communications: order and shipping e-mails) handled 256,313 business requests in the week to 19 Nov 2026 and failed 3,076 of 256,313 (1.20 %). All of them are on its one operation:

| Operation | Calls | Failed | Failure rate | p95 |
|---|---|---|---|---|
| `send_email` | 256,313 | 3,076 | 1.20 % | 746 ms |

- **`send_email` fails 3,076 of 256,313 (1.20 %)** in the week to 19 Nov 2026 — about 439 a day. Each failure is a user or an operator who has to retry, or work that is not done.
- **No monitor watches `notify-worker`**, so none of these failures pages anyone.

**Actions**
1. Add a failure-rate monitor on `notify-worker` — it has no monitor today — and alert when `send_email` fails more than 37 times in an hour (about 2.0× the hourly average in the week to 19 Nov 2026, which is 18), and lower the threshold as the fix lands.
2. Pull one day of `send_email` failures from the application log and classify them (validation, data, timeout, dependency).

> ➕ **live graph** — `/timeseries` · `sum:trace.servlet.request.hits{service:notify-worker,resource_name:send_email}.as_count()` (requests); `/timeseries` · `sum:trace.servlet.request.errors{service:notify-worker,resource_name:send_email}.as_count()` (failures, as red bars)

*Since last month: failure rate of `send_email`: 1.20 % → 1.20 % (→ flat).*

---

## 🟠 Finding 8 — Observability foundations are thin

Three gaps in how the estate is instrumented and tagged. Not one of them hurts a user today; each makes the next incident longer.

| Gap | Evidence | Why it matters |
|---|---|---|
| Monitors on 2 of 4 services | none on `catalog-api` and `notify-worker` | a service can fail silently |
| No SLOs | the SLOs column is empty for every service | no shared definition of "good enough" |
| Scanner traffic in APM | 4 of 7 endpoints on `checkout-api` are probes | noise in the endpoint catalogue and error rates |

**Actions**
1. Add failure-rate and latency monitors to `catalog-api`.
2. Agree SLOs for the customer-facing services and review them monthly.
3. Ingestion hygiene: exclude scanner traffic.

*Since last month: foundation gaps: 3 → 3 (→ flat).*

---

## 📝 Also noted

- 🟠 **Vulnerability scans reach `checkout-api`** (4 probe endpoints, none answered) — *Block scanner traffic at the edge*
- 🟠 **Servers call the public internet** — `www.youtube.com` — *Allow-list egress*

---

## ✅ Recommendations, in order

| When | What | Why |
|---|---|---|
| **Now** | Remove or repoint the `uat-pricing.acme.internal` reference in production | Finding 1 — Production calls a UAT endpoint |
| **Now** | Pull one day of `POST /pay/authorise` failures from the application log and classify them (validation, data, timeout, dependency) | Finding 2 — One in twelve card authorisations fails on Payment gateway; refunds… |
| **Now** | Alert when `POST /pay/authorise` fails more than 380 times in an hour (about 2.0× the hourly average in the week to 19 Nov 2026, which is 186), and lower the threshold as the fix lands | Finding 2 — One in twelve card authorisations fails on Payment gateway; refunds… |
| **Now** | Pull one day of `POST /api/checkout` failures from the application log and classify them (validation, data, timeout, dependency) | Finding 3 — One in twenty checkouts fails on Checkout API |
| **Now** | Alert when `POST /api/checkout` fails more than 220 times in an hour (about 2.0× the hourly average in the week to 19 Nov 2026, which is 110), and lower the threshold as the fix lands | Finding 3 — One in twenty checkouts fails on Checkout API |
| **Now** | Assign an owner to `legacy-erp.acme.internal` and agree a fix or a retirement date | Finding 4 — One dependency is failing |
| **Now** | Retire the calls to `legacy-erp.acme.internal` or restore the service | Finding 4 — One dependency is failing |
| **Now** | Add a failure-rate monitor on `notify-worker` — it has no monitor today — and alert when `send_email` fails more than 37 times in an hour (about 2.0× the hourly average in the week to 19 Nov 2026, which is 18), and lower the threshold as the fix lands | Finding 7 — 1.20 % of `send_email` calls fail on Notifications, and nothing is… |
| **Now** | Pull one day of `send_email` failures from the application log and classify them (validation, data, timeout, dependency) | Finding 7 — 1.20 % of `send_email` calls fail on Notifications, and nothing is… |
| **Now** | Add failure-rate and latency monitors to `catalog-api` | Finding 8 — Observability foundations are thin |
| **Next** | Track card authorisations against the agreed 99.5 % success target as a Datadog SLO, so the burn rate pages before the target is missed | Finding 2 — One in twelve card authorisations fails on Payment gateway; refunds… |
| **Next** | Cache the token in each client of `payment-gateway` until shortly before it expires — that removes at least 726,840 calls in the week to 19 Nov 2026 (the excess over one token per business call) and up to nearly all 1,105,404 | Finding 2 — One in twelve card authorisations fails on Payment gateway; refunds… |
| **Next** | Track checkouts against the agreed 99.5 % success target as a Datadog SLO, so the burn rate pages before the target is missed | Finding 3 — One in twenty checkouts fails on Checkout API |
| **Next** | Turn on the Datadog continuous profiler for `payment-gateway` for a day — it shows wall time by function, including time the traces do not cover | Finding 5 — Payment gateway is slow on two of its features |
| **Next** | Profile `GET /api/search` on `catalog-api` (query plan, downstream calls) to bring it under its agreed 2.00 s p95 | Finding 6 — One slow operation in Shoppers — web and app: `GET /api/search`… |
| **Next** | Ingestion hygiene: exclude scanner traffic | Finding 8 — Observability foundations are thin |
| **Later** | Set a latency target for the slow operations of `payment-gateway` (for example p95 under 2.00 s) and track it as an SLO | Finding 5 — Payment gateway is slow on two of its features |
| **Later** | Agree SLOs for the customer-facing services and review them monthly | Finding 8 — Observability foundations are thin |

---

## 📎 Appendix

**Data analysed.**
- APM Endpoints list `endpoints.csv`: 15 endpoints, 7 days to 19 Nov 2026 — 7 days as stated for this review, consistent with the health-check cadence: `GET /actuator/health` on `catalog-api` 201,600 requests = one every 3.000 s; `GET /actuator/health` on `checkout-api` 201,600 requests = one every 3.000 s (the cadence also fits 14 and 28 days, so it cannot confirm the range).
- Inferred services `inferred.tsv`: 6 hosts and entities the traced services call; volumes are in the Services-list export, about 6 days.
- APM Services list `services.tsv`: 4 services (p95, monitors, SLOs, env tags; its volumes cover about 6 days, so they are not mixed with the Endpoints list).
- Trace sample `spans-2026-11-19.csv`: 20,328 spans, 2026-11-19 02:00:00–02:09:00 UTC (9 minutes).

**Definitions.** *Failed* = a request that returns a 5xx or records an application error; 4xx responses are not failures. *p95* = 95 % of requests finish within this time. Service p95 comes from the APM Services list. Request volumes are APM trace metrics, which count every request regardless of trace sampling. Entry requests in the span sample were identified by the rule *a root span, or a span whose parent belongs to another service (integration libraries and host names excluded)*.

**Noise excluded from business totals** (every exclusion, with the rule that made it):

| Class | Rule | Source | Endpoints | Requests removed | Failures removed |
|---|---|---|---|---|---|
| health | built-in health pattern | builtin | 2 | 403,200 | 0 |
| scanner | built-in scanner pattern | builtin | 4 | 98 | 98 |

**All traced services** (requests in the week to 19 Nov 2026; failure % on business operations; p95 from the Services list):

| Service | Requests | Failure % (business) | p95 | Monitors | Stack / operation |
|---|---|---|---|---|---|
| `catalog-api` (Catalog API) | 4,690,503 | 0.13 % | 4.39 s | 0 | Java servlet |
| `checkout-api` (Checkout API) | 1,805,201 | 1.30 % | 1.67 s | 2 | Java servlet |
| `payment-gateway` (Payment gateway) | 1,483,968 | 2.80 % | 2.66 s | 1 | Java servlet |
| `notify-worker` (Notifications) | 256,313 | 1.20 % | 746 ms | 0 | Java servlet |

**Caveats.**
- times are shown in UTC (the timezone 'Europe/London' could not be applied on the machine that built this review)
- the Services list volumes cover about 6 days against 7 days for the Endpoints list (median ratio 0.86); rates and p95 are comparable, volumes are not

**Method.** Every figure above was computed from the exports by the review generator (ddreview 1.0.0) and recorded in `facts.json`; last month's `facts.json` (October 2026) provided the comparison. Mermaid diagrams are drawn from the same tables. The generator writes a first draft in one command; read every finding before sharing.
