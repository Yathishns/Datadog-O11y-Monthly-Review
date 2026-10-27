# ddreview — Datadog exports → monthly Observability Review notebook

One command turns Datadog CSV exports into a Datadog **notebook** (Markdown with Mermaid diagrams) that reads like a
consultant's hand-written monthly review: an executive summary, a service map, findings with evidence tables, impact,
actions and live-graph recipes, Now / Next / Later recommendations, and an appendix that says exactly what was analysed.
Nothing about a client lives in the code; the client's vocabulary lives in one `org.json` that the tool drafts from the data.

- **Python 3.9+ standard library only.** No pip install. Works on Windows, macOS and Linux.
- **Spans export is enough.** Each extra export (Endpoints list, Services list, inferred services, resource catalog) unlocks
  more; the appendix says what each missing export would have added.
- **Every number is traceable.** Rates are stored as numerator / denominator / percent in `facts.json`; a span sample is never
  scaled up to a day or a week; per-day figures appear only when the Endpoints-list range is known (section 5, `window.days`).
  Failure figures appear only when the exports carry a failure signal (a status code or an error flag).
- **Deterministic.** Same inputs and config → byte-identical outputs. Keep `facts.json`; next month's build compares itself
  to it and says what closed, what is new, what moved.
- **Mermaid two ways.** The diagrams are generated from the evidence tables; the same tables are written as prompt files for
  an LLM when you prefer that route, and `validate` checks whatever comes back.

## 1. Export from Datadog (15 minutes)

| Export | Where in Datadog | Notes |
|---|---|---|
| **Spans** (required) | APM › Traces › *Spans* list view › export / Download as CSV | Keep the **Date** column. Add these columns before exporting if you can: **Trace ID, Span ID, Parent ID, Operation Name, Type, Env, Host, Status Code** (and `peer.service` if you use it). Export spans of every type (no span-type filter), at a busy hour, filtered to production (`env:prod`). The export is capped (observed 100,000 rows ≈ minutes of traffic): it is a *sample*. |
| APM Endpoints list | APM › Software Catalog › Endpoints, time range **7 days**, Export | Complete weekly volumes and per-endpoint failure counts. Set `window.days` to the range you selected. Health-check counts are only a consistency check: a fixed cadence fits 7, 14 and 28 days (and sometimes 1) equally, so it cannot catch a wrong range; check that the probe interval the appendix reports is your real one. |
| APM Services list | APM › Software Catalog › Services, Export (tab-separated) | Monitor and SLO coverage, env tags, service p95. Export it twice: once as is, once with *inferred services* shown (the hosts your services call). |
| Resource catalog (optional) | Infrastructure › Resource Catalog, Export | Tag coverage only. |

Put all files of one month in one folder. File names do not matter: each file is recognised by its header row. Two span
exports that cover different windows are pooled and flagged in the appendix, and each file is read with its own columns (a
file without Parent ID is never treated as a tree because another file has one); a span export and its re-export with
Span IDs added are counted once, and the richer copy is the one kept. Two Endpoints lists, Services lists or inferred lists that repeat a row (a left-over from last
month, or the same services exported for another environment) stop the build with a message naming both files, because the
review is production-scoped; lists of different services are merged. Files are read as UTF-8; a spreadsheet re-save in
Windows-1252 is detected, and a European re-save (semicolons, decimal commas) is read correctly.

## 2. Run (three commands)

```bash
python -m ddreview inspect exports/
```
Prints how each file was recognised, which column maps to what, the units it decided on (and whether it had to infer them),
the rows it skipped and why, the time window, the entry-span rule it will use, the capabilities unlocked, the detectors that
would be skipped, and what to export next time to unlock more.

```bash
python -m ddreview init exports/ --org "Client Name" --config clients/client/org.json
```
Drafts the per-org config from the data: domains by service family, node groups, operation names for the live graphs,
the busiest operations as named KPIs, operations that fail on every call (listed under `noise._by_design_to_confirm`;
they stay reported as failures until you move a confirmed one to `noise.by_design`) and, when there is an Endpoints list,
a `window.days` to fill in. Every guess is marked `_todo`. It also writes `prompts/20-name-domains.md` beside the config — paste it into an
LLM to get domain names, audiences and tiers as JSON. Ten minutes of editing — name the domains, pick each tier, give each
KPI a plural business name ("claim creations"), set `window.days`, delete wrong guesses — and the client is set up for good.
Re-running `init` reads the existing config (its column aliases and service rules included), keeps what you edited, fills
in any section the file lacks from a fresh draft (so a file holding only `columns.aliases` becomes a full config) and lists
services that appeared since.

```bash
python -m ddreview build exports/ --config clients/client/org.json --month "November 2026"
```
Writes `out/<org>/<YYYY-MM>/`:

| File | What it is |
|---|---|
| `<org>-observability-review-<YYYY-MM>.md` | the notebook — import it (section 3). Named `.INVALID.md` when validation found an error, or any warning under `--strict` |
| `facts.json` | every figure, with numerator and denominator, plus the ledger of printed numbers; keep it for next month |
| `mermaid/*.mmd` | the diagram sources (also embedded in the notebook); not written with `--diagrams prompt` |
| `prompts/*.md` | aggregated tables + instructions for an LLM (list below) |
| `mapping-report.md` | how every export column was read, which unit was assumed, which rows were skipped |
| `validation.txt`, `build-summary.txt` | self-checks, analyst notes (config to-dos the notebook never shows) and a short deterministic summary with sha256 of the outputs |

The prompt files: `00-README.md` (index, always); `01-diagram-landscape.md` (unless `--diagrams code`);
`03-diagram-dependencies.md` (only with a failing-dependency, non-production or egress finding);
`04-diagram-inside-<service>.md` (one per finding with an inside-a-request diagram); `10-polish-findings.md` and
`11-exec-summary.md` (always); `22-month-over-month.md` (only when an earlier month was found).

Diagrams: `--diagrams both` (default) embeds the generated Mermaid and also writes the diagram prompts; `code` embeds the
generated Mermaid and writes no diagram prompts; `prompt` embeds no diagrams and writes no `mermaid/` folder — the notebook
carries a visible marker where each diagram goes, to be replaced by the LLM's answer (so `prompt` refuses `--no-prompts`).
The requests-by-domain pie is drawn only by the generator.

Options: `--as-of 2026-11-19` (defaults to the latest timestamp in the spans export; a spans export is recognised only with
a Date column or a Trace ID / Span ID column, and without Date pass `--as-of` — `--month` alone dates the review the 1st of
that month and the sample window is unknown), `--out DIR` (default `out`), `--previous auto|PATH|none` (any case; default:
the config's `previous`, else auto, which finds the latest earlier month under `out/<org>/`), `--window-days 7` (overrides
`window.days`), `--max-findings 8`, `--diagrams both|code|prompt`, `--no-prompts`, `--strict` (warnings fail the build),
`--force` (rebuild a month folder: the earlier notebook, `mermaid/*.mmd` and `prompts/*.md` are removed first),
`--org "Name"` (when there is no config yet; with a config it only changes the display name).

Exit codes: 0 ok · 1 validation errors, or any warning under `--strict` (the notebook is still written, as `.INVALID.md`) ·
2 input error · 3 config error.

Other commands: `python -m ddreview diff out/x/2026-10/facts.json out/x/2026-11/facts.json --config org.json` prints the
month-over-month tables. `python -m ddreview validate notebook.md --facts facts.json` checks a notebook's structure,
Mermaid blocks and numbers. For LLM-polished text, check the answer before pasting it:

```bash
python -m ddreview validate answer.md --facts facts.json --text --against notebook.md
```

`--against` compares the numbers of each section (matched by heading) with the same section of the original: a number added
or changed is an error, a number dropped is a warning. `--text` (which needs `--facts`) only rejects a figure that
`facts.json` cannot produce in any format, so on its own a typo that happens to match another fact passes. Neither checks
values inside a Mermaid block; read a pasted diagram against the tables.

## 3. Import into Datadog

Notebooks → New Notebook → **⋮ → Import markdown file (.md)**. If the page looks empty afterwards, reload it or open
⋮ → Version history; failing that, add one Markdown cell (`/markdown`) and paste the whole file — headings, tables and the
Mermaid blocks render in a single cell. (The import path is practitioner-confirmed; Datadog's docs only describe the API:
`POST /api/v1/notebooks` with a `markdown` cell carries the same text.) Then add the live graphs from each finding's
`➕ live graph` recipe with `/timeseries` or `/toplist`, and ⋮ → **Download as PDF** to circulate.

## 4. Read before you send

The kit writes a first draft in one command, not a document to publish unread. Each month:

1. Read every finding against the numbers; close the ones that no longer stand, add at most a couple of new finding types.
2. Read `build-summary.txt` for the analyst notes (live-graph recipes still carrying the `OPERATION` placeholder, an unset
   `window.days`, domains not yet named, operations that fail on every call) and check the appendix: services that fell
   into *Other services* and the caveats (an ambiguous window, skipped rows, a mixed or non-production export, outbound
   calls that were not counted as requests).
3. Take the exports on the same weekday and hour as last month, with the same 7-day range, so the months compare.
4. Keep `facts.json` and `org.json` under version control per client.

## 5. The config (`org.json`)

JSON; keys starting with `_` are ignored, and a `null` means "use the default" (except `window.days` and `previous`); an
unknown key — at the top level or inside a domain, operation or ratio — is an error that names the nearest valid key, and a
value of the wrong type (a pattern list written as a single string, say) is a config error (exit 3). The domain key `other`
is reserved for services outside the named domains. The file may be saved as UTF-8 (with or without a BOM) or UTF-16. Thresholds not set here come from `ddreview/defaults.json`, where the pattern libraries also live
(health checks, scanner probes, static assets, framework events, token endpoints, telemetry hosts, cloud suffixes,
non-prod markers).

| Key | Purpose |
|---|---|
| `org` | `name`, `short` (paths), `prepared_for`, `prepared_by`, `timezone` (IANA; on a machine without tzdata a zone with no daylight saving, such as `Asia/Hong_Kong`, keeps its own offset and any other zone uses `utc_offset` such as `"+01:00"`; with `utc_offset` left at the drafted `+00:00` the times are shown in UTC and the notebook says so), `production_envs`, `treat_missing_env_as_production` (default true), `internal_domains` (`init` lists its guesses under `_internal_domains_to_confirm`; move only the client's own domains, since a domain listed here is never public egress) |
| `report` | `title_template` (may use `{org}` and `{month}` only), `max_findings`, `diagrams` (`both` / `code` / `prompt`), `delivered_alongside`, `closing_note`, `thesis` |
| `window` | `days`: the range selected when exporting the Endpoints list. A health check's cadence fits multiples such as 7, 14 and 28 days equally, so the kit cannot derive it; without it no per-day figure is quoted. A probe count supports a range only when it lands on a real probe period (1–60 s) for some number of probers, one cadence per service; a range is proved, or a stated one contradicted, only when two services agree. A stated range the cadence contradicts is reported and per-day figures are withheld. `--window-days` overrides |
| `domains` | list of `{key, label, icon, audience, tier, services [regex], description}`; `tier` is `customer`, `clinical`, `staff` or `internal` — red findings need customer or clinical (staff needs a stronger signal). A domain that matches no service is reported, not shown as healthy |
| `services` | `display` names, `operations` (to override the span operation the review detects per service and per operation; `init` lists what it saw under `_detected_operations`; span operation name for `trace.<op>` metrics, e.g. `servlet.request`; a value written as a metric such as `trace.servlet.request.hits` is read as `servlet.request`; keys are exact service names or full-match regexes, an exact name wins and otherwise the first matching pattern), `default_operation`, `node_groups` (regex per group), `integration` (pseudo-services that are never entry points), `include` / `exclude`, `aliases` (`{old: new}` renames so month-over-month joins). An `include` / `exclude` that leaves no service is a config error (exit 3); the services it leaves out are listed for the analyst. Names ending in `-v1`, `-v2` are API versions and are never grouped as nodes |
| `operations` | named business operations (KPIs): `{key, label, service, match, critical, slo_success_pct, slo_p95_s}`; the label is a plural noun phrase ("claim creations", so "one in twenty claim creations fails"); `slo_success_pct` is a percentage (99.5, not 0.995); an operation within its own `slo_p95_s` is left out of every slow finding (its table, its share, the "slowest" clause and the monitor threshold); every operation in a failure table whose KPI misses its `slo_success_pct` is reported against that target, and a KPI that misses it while failing under `fail_amber_pct` gets an amber finding of its own (from a span sample only when the excess passes `mom_min_failures` and `mom_rate_z`; a KPI spanning several services is judged per service; only business operations count, never by-design or health ones); an operation slower than its own `slo_p95_s` is a slow operation even under `slow_op_s`; the achieved rate is printed with enough decimals that a missed target never reads as met; a KPI label names a failure rate only when the KPI matches that one operation |
| `ratios` | call-ratio pairs such as token requests per business call: `{label, numerator {service, match}, denominator {service, match}, expect_max}`. Both sides count operations of traced entry services (a side that matches nothing is named in the analyst notes); when the two sides sit on different exports, both are counted from the span sample. The first 40 characters of each label must differ (they make the finding id). A configured ratio keeps its own wording when it joins another finding, and it replaces the automatic token ratio only when its numerator counts token calls |
| `noise` | extra `health`, `scanner`, `static`, `framework` patterns; `by_design` failures `{service, match, why}` (the appendix lists each pattern with its why, and the noise finding calls it the team's decision; `init` drafts the why as `_todo`, which is never printed); `not_noise` overrides. A bare page extension (`.aspx`, `.php`, `.jsp`) is the app's own page type, never a scanner probe. Built-in health checks are bare probe and scrape paths (`/healthz`, `/readyz`, `/livez`, `/health/ready`, Quarkus `/q/health/live`, `/metrics`, `/q/metrics`, `/prometheus`, `/management/prometheus`, `/actuator/prometheus`, the gRPC `grpc.health.v1.Health/Check`; `/health` after up to four plain segments, never after an id; .NET routes without a leading slash such as `GET api/v1/health` too), and a metrics scrape never proves the export range; built-in scanner probes are Tomcat and JBoss default apps at the path root (`/manager/html`, `/docs/`, `/examples/`, `/jmx-console`), probe verbs only as upper-case HTTP verbs (`TRACE /`, `PROPFIND /`) and well-known probe signatures (`/phpmyadmin`, `/.aws/`, `/.env.*`, `/server-status`) |
| `hosts` | `nonprod_markers`, `telemetry`, `allowed_egress`, `internal_suffixes`, `production` (patterns for hosts that are production whatever their name says, such as a partner's `uat.` host the client calls in production). Built in: Datadog's own intake hosts are allowed egress, never "a second tool", and SaaS hosts named `dev.` (`dev.azure.com`, `*.dev.azuresynapse.net`) are production |
| `failure` | `rule`: `5xx_or_flag` (default), `flag_only` (needs the Error column), `non_2xx`; the notebook states the rule in use, and says that weekly figures from the Endpoints list keep Datadog's own error count whatever the rule |
| `thresholds` | any value from `defaults.json`, e.g. `"fail_red_pct": 3.0` (the findings cap is `report.max_findings`). Count thresholds are per 7 days of Endpoints-list data and scale with the exported range (by the longest range the cadence fits when the range is not confirmed); month-over-month rate changes must also pass `mom_min_failures` and a two-proportion test at `mom_rate_z` |
| `columns` | reader overrides: `aliases` (header → canonical column name such as `duration` or `ts`; also used to recognise a file), `units` (header → ns/us/ms/s; a unit written in the header, such as `Duration (sec)` or `duration_us`, is read without it), `date_format` |
| `prompts` | `mask_resources` (default true): mask identifiers in resource names inside the prompt files |
| `previous` | `auto`, `none` (any case), or a path to last month's `facts.json` (relative to the config file) |

`examples/acme-retail/org.json` is a complete example.

## 6. What the findings are

Fourteen detectors, each with a healthy branch (a domain with nothing wrong gets a ✅ row; one with too little traffic a ⚪
row; one whose failures cannot be measured from the exports a ⚪ "unmeasured" row; one whose small operations fail a 🟠
"review" row — never silence) and a stable id so month-over-month is a join: failing operations · slow services (with the
fixed-delay signature and the inside-a-request diagram) · slow operations · failing dependencies · production calling
non-production hosts (always red) · noise masking failures · node imbalance · token reuse (call ratios) · work inside a
request (statements, HTTP calls, secret reads per request; N+1; scheduler bursts) · scanner exposure · public egress and
telemetry · client errors · observability foundations · regressions since last month. Severity: red when customers or
patients are affected now, amber for productivity, cost and hygiene.

Month over month compares like with like: a finding's trend is last month's value of the same metric on this month's
subject (the same operation, host or node), "no comparable figure" when there is none, volumes only when both export
ranges are known to match, and failure rates only when both months record failures. A failure finding this month's
export cannot measure is "not measured this month", not closed; one last month's export could not measure is "first
measured", not new; one that closed only because a threshold it uses changed says so, and the changed thresholds are
listed. A finding whose operation, slow operation or dependency fell below its call floor reads "too little traffic to judge",
one whose operation you moved to `noise.by_design` reads "reclassified as by-design noise", one still reported in *Also
noted* or merged into another finding reads "now reported within another finding", and one whose service or caller is
missing from this month's span sample (or whose inferred list was not exported, or whose hosts this month's export does not
name) reads "not checked this month" — never "closed". An operation or host missing from a short span sample reads "too
little traffic to judge". "Below this month's threshold" is used only when the figure still clears last month's bar and the
changed threshold alone excludes it. Hosts that last month's exports could not see (no inferred list, no host names) are
"first measured", not new; a finding merged into another last month persists. Calls named in one month and unnamed in the
other are matched by caller and kind of call (database, HTTP, cache, queue), summed over every caller. Slow-service and
work-inside-a-request findings below their own floors read "too little traffic to judge"; a figure that still clears the
bar never reads "closed". A dependency named after its integration library (`<app>-http-client`, `postgresql`) and later
by peer.service is the same dependency; unnamed calls never prove or disprove a non-production or public host, so those
findings read "not checked" or "first measured". When `failure.rule` changes between months, failure figures are not
compared and the notebook says why. `diff` without `--config` uses the thresholds and aliases the build recorded. A
service's failure rate is compared only when both months judged the same operations, a work-inside-a-request finding tracks
the figure that raised it, an outlier-only slow finding tracks its operation's p95, and a p95 from fewer than 200 sampled
requests never becomes a move or a regression. A sampled p95 (a service's or an operation's) moves only when the two
months' 95 % order-statistic intervals do not overlap, and a rate with no calls in either month is never compared. A finding
that stops only because its operation is now within its agreed `slo_p95_s` reads "closed — within its agreed p95 target",
and changed agreed targets are listed beside changed thresholds; an outlier-only finding is "now reported within another
finding" only when that finding names its operation. A finding from a detector last month's exports could not run is
"first measured". The public-egress host count is compared only when both months could see the same hosts. The caveat that
findings changed identity counts only regroupings, findings whose subject is gone and findings about services new this
month, never a genuine close or a new problem. Without an earlier month, the appendix says why (`--previous none`, or no
earlier `facts.json` under the out folder, which the analyst notes also flag) and never claims a first review. A slow
share is compared on this month's basis (last month's operations measured with this month's slow bar and agreed targets),
and when an agreed target changed on a service its findings' severities are not compared. A service's failure rate is
compared on the operations both months class as business, so moving an operation to `noise.by_design` never reads as an
improvement. A non-production or egress finding that stops only because `hosts.production` or `hosts.allowed_egress` now
covers its still-called hosts reads "reclassified by the hosts config". Shares measured on the sample (repeated calls, node
traffic gaps) must pass the two-proportion test, and a slow share that moved only because sampled p95s crossed the bar
within their noise is flat. A node missing from this month's exports reads "not checked this month". A finding that closes
only because its agreed target changed (success or p95, a target removed included) reads "closed — within its agreed
target"; a target kept under a renamed KPI key is no change. A sampled failure or slow finding that falls under the bar
within the sample's noise reads "under the bar, within sampling noise", not closed. A host allow-listed or reclassified
by the hosts config is no comparable change in the egress count, and a finding that appears only because of that
reclassification is "first measured". When last month's headline operation is now listed as noise, the persisting row
says so, and What moved notes when a service's rate was compared without operations one month counts as noise. A close
reads "within sampling noise" only on a noise test: overlapping p95 intervals (or a weekly p95 inside the other month's
sampled interval), or a failure count or slow-share change that fails the two-proportion test; a real fix on a small sample
closes. A slow operation reported last month inside the slow-service finding persists (and that finding is now reported
within it). A scanner finding on a span sample is "too little traffic to judge" when the sample caught no probe. A KPI is
compared under a renamed key, never when its operations changed, and a dependency never when its class changed. A
reclassified operation names its new noise class (health-check, by-design, ...), slow findings included, and a service's
p95 is not compared across such a move. Client-error, noise, node and scanner findings keep their detectors' floors
("too little traffic to judge", "below the detector's call floor"), and a noise finding that appears only because an
operation was reclassified is "first measured". A service alias whose new name already existed last month
joins the two (counts add up, rates are recomputed); "outbound calls of <service>" last month and a named host this month
from the same caller are one finding that persists.

Entry spans: a span that says it serves (Type web/server, span.kind server/consumer, or a root span) is a request even when
the service name looks like a host (a dotted .NET name). In-process framework spans (`spring.handler`,
`aspnet_core_mvc.request`, middleware) are never a second request. Outbound calls whose host is not recorded become a
dependency named "outbound calls of <service>" (with or without Parent ID and Type), so their failures still reach the
findings; the notebook then points to the calling service's Dependencies page, since the pseudo-name has no page of its own.
Queue client libraries (`<service>-kafka`, `-rabbitmq`, `-sqs`, `-servicebus`) are integrations when they produce, and the
consuming app when their span consumes (`kafka.consume`, span.kind consumer) — .NET names a worker's consume span after the
library — so a worker's work is never charged to the producer. A queue span whose operation consumes (`kafka.consume`,
`celery.run`) is an entry even without span.kind, and a Services-list-only service of type queue is a consumer app. The
entry rule is decided per row, so row order never changes the totals; with Type but no Operation Name, a server-typed span
with no method and no status beside the service's own requests is in-process framework work. Calls are joined per file:
by parent and child where the file has Parent ID, else by peer tag, else by trace. Per-request anatomy counts only requests
whose child spans can be told apart, and a finding quotes how many those were. A service counted from the span sample
whose spans carry no status code or error flag shows "failures not recorded", never "0 % failed" — in tables, finding
text, summaries, node tables and prompts — and a domain that mixes recorded and unrecorded services quotes its rate with its
own base ("8 of 2,000 requests with a failure signal") and names the others. Under `failure.rule: flag_only` only the Error
column counts as a failure signal. Calls of an integration that no join could attribute (no Parent ID, no peer value, a file
with no join column) are counted without a caller rather than dropped, and calls made by a service `services.exclude`
leaves out are not counted. A job a service runs from its own queue (Laravel web and worker sharing a name) is an entry; a
library-named span below another service (`<app>-graphql`, `celery-producer`, botocore's `aws.s3`, Node's `<app>-aws`) is
that service's entry only when it shows it serves; an app that both produces and consumes through `<app>-kafka` keeps its produce calls as a dependency of the
producing request. With an Env column, a non-production host is judged on the calls made from production-env spans (a
service running under one name in prod and uat counts only its prod calls). A consume span the tracer names after the
library (`kafka`, Java/Go/Python v0) runs the consuming app named by the spans below it; a span directly below another
service's client call (a Python gRPC server) is that service's entry. A request records its failure on its own entry span
only, never through its outbound calls; when the span sample cannot judge a worker, its Services-list failure rate is quoted
instead. A dependency's p95 covers every caller's calls, callers whose calls fail are named first, and a client span whose
parent is missing still counts toward the host its peer tag names. Each operation's live graph uses that operation's own span
operation (a queue job's metric is not the web endpoint's), and a consumer renamed from the library keeps the tracer's
service tag (`trace.kafka.consume{service:kafka}`). A consumer is named after an app in its own process only (never a
library such as `hibernate`, never a service reached through its produce or client calls), the same for every message of a
topic. A gRPC server or Kafka listener span is never read as a client call. In-process method spans (`trace.annotation`,
`repository.operation`, `hibernate.*`) are not outbound calls. A worker whose spans record no failures is judged on its
Services-list failure rate (marked ◇) when the list was exported; a traced health check never makes its failures known.
Endpoints-list figures always carry Datadog's error count, whatever the span sample records. A slow operation within its
agreed `slo_p95_s` makes neither a slow-operation nor a slow-service finding. A worker on the Endpoints list only through
its health check is counted from the span sample, never with weekly probe counts added. The inferred services list on its
own is caller data for failing dependencies and public egress. A repeated child is the same call to the same target (host
or peer), so a request that calls six hosts once each is fan-out, not N+1; one message consumed by several consumer groups
is a call into each. The last label of a host is its top-level domain, never an environment marker (`.dev`, `.qa`).
HTTP calls are told apart by peer, host or the service whose entry span they reach (a BFF calling six services once each
is fan-out, worded as such), while repeated database or cache calls stay N+1 whatever node they hit. A worker on the
Endpoints list only through probes and absent from the span sample keeps its Services-list figures. A Services list
exported over hours is described in hours. Live graphs select the finding's own operation by its exact `resource_name`
tag (`post_/api/claims`, `get_/api/cart/` for an obfuscated id, `/inventory.v1.inventory/getstock` for gRPC), never a word or a
prefix that matches its siblings. A worker whose only sampled spans are its health probes keeps its Services-list figures.
The slow share is a share of all the service's business requests (operations too small for a p95 count as not slow). The
claim that slow requests show no child spans needs an export that carries child spans. A client call whose callee's entry
span is in the trace is one edge, to that service, even when peer.service names its DNS host. Fan-out is judged per call
target on the requests the anatomy examined, and a fan-out-only finding gets a fan-out action. An env with a region
suffix of up to four parts (`prod-eu`, `prod-us-east-1`) counts as production when `prod` does, unless the suffix carries
a non-production marker (`prod-staging`). An operation that two KPIs match stays with the first one in org.json, in its
figures as well as its label, and the build says so. A production call through a non-production host stays a
non-production dependency even when the UAT callee is traced in the same export. A resource that is only the verb
(`GET`) keeps that name. A service with no business request in the sample has nothing to judge, which is not 'failures
not recorded'. Client services named after their store (`redis.client`, `elastic.client`) are caches and databases, and
verb-only calls (`GET`) to hosts the export cannot name are never called N+1. Fan-out wording needs the once-called
targets to explain the per-request figure. With a peer.service column in the export, the hint asks the tracer to set it. Footnotes say why each service is counted apart (off the Endpoints list, on it only
through probes, in the sample only through its health probes, in the sample without a request marker).

Diagrams: each caller's edge in the dependencies map carries that caller's own failure rate and p95 (a healthy caller of a
failing host reads "no failures"); a host is red only on its own figures, never because an estate-wide finding about other
hosts is red; the slow-edge label uses `dep_slow_s`; domains holding a flagged service get their place on the service map
first, and every "+N more" node takes the worst colour of what it hides. A stack word on the service map is shortened at a
word boundary (`.NET (ASP.NET Core)` → `.NET`) or left out, never cut in the middle; the landscape prompt's Colour table
folds exactly the flagged hosts the generated map folds, and the dependencies prompt's "+N more hosts" row carries the worst
colour of the hosts it hides. When flagged hosts do not all fit, the map keeps red before amber, then the busiest. The
landscape prompt quotes each domain's failure rate as the notebook does (◇ Services-list rates, "—" with no business base),
the dependencies prompt gives each non-production host its own marker (`production → staging`), the inside-a-request marker
stands on its own line, and `validate` rejects a code fence that does not start a line. HTTP-client pseudo-services
are drawn as hosts and queues as queues, never as databases. `validate` checks an answer an LLM wrapped in a code fence as
the text inside it, treats an `--against` check that matches no section as an error, and warns about any diagram marker
left in the notebook.

What the kit never does: scale a sample to a day or week, quote an hourly alert threshold from a sample, print a failure
rate the exports cannot support, claim a service has no monitors unless a Services or Endpoints export says so, claim no
egress unless the data can show hosts, or present the worst endpoint's p95 as the service's p95 (it says "worst busy
endpoint" when that is all it has).

## 7. Examples and tests

```bash
python examples/make_samples.py --previous     # three fictional orgs with different stacks, plus a previous month
python -m ddreview build examples/acme-retail/exports-previous --config examples/acme-retail/org.json --month "October 2026" --previous none --force
python -m ddreview build examples/acme-retail/exports --config examples/acme-retail/org.json --month "November 2026" --force
python -m unittest discover -s tests
```

`out/` ships with these builds, so `--force` is needed to rebuild them (or pass `--out out-demo`). The November build
finds the October `facts.json` and adds *Since last month*.

`acme-retail` is Java/Spring with full trace ids and all four exports; `northbank` is .NET with spans + endpoints and no
trace ids; `quickship` is Node/Express with the six default span columns only — the notebook degrades honestly for each.
`tests/test_variants.py` covers the export shapes that bite in practice: bare nanosecond durations, a `Status` column of
ok/error words, semicolon delimiters with a BOM and `@`-prefixed headers, overlapping exports, a Windows-1252 re-save,
host-named services, empty service cells, a service literally called `end`, a rename across months.
`tests/test_round2.py` to `test_round14.py` cover what thirteen adversarial reviews found: error flags,
unit words in headers, European number formats, root spans with Parent ID 0, dotted .NET names, client and in-process
operations, span.kind, re-exports, per-request anatomy, outages that stay visible, Services-list-only services,
month-over-month subjects, statuses and sampling noise, `validate --against`, `--force` clean-up, diagram budgets and
config type errors, per-file columns, row-order independence, call floors and aliases across months, per-caller edge labels
and per-host colours, digits in config text and drafted domain keys, queue consumers, pooled files with different
columns, configured ratios, services filters, failure rules, number words under `--against`, diagram budgets with many
domains, unjoined integration calls, queue jobs and producers, failure signals per rule and per service, count words under
`--against`, month-over-month floors and host matching, env-based production calls, library-named consumers, entry-span
failure signals, dependency p95 across callers and failure-rule changes, Services-list rates for workers, consumer naming
per topic, method spans, agreed p95 targets and per-operation month-over-month continuity, page extensions and library
pseudo-services, declared production hosts, agreed targets in slow tables and KPI subjects, 'within target' closes, p95
sampling intervals, detectors last month could not run, fixed-offset zones, by-design rules and the landscape colour table,
health-check-only workers, probe and scanner patterns, missed agreed targets under the bars, month-over-month bases for slow
shares, reclassified operations and hosts, fan-out and consumer groups, and fences, cadence proofs, framework probes,
target misses within noise and per service, relaxed and removed targets, closes within noise, allow-listed hosts and
fenced answers, probe periods, framework scrapes, probe-only workers, the slow-share base, one edge per call, fan-out per
target, persisting slow operations, quiet scanner samples, KPI operation changes and CRLF answers, cloud-region envs,
overlapping KPIs, UAT callees, probe-sampled workers, store-named clients, detector floors month over month and p95
targets in milliseconds (a config error).

## 8. Data handling

The exports and `facts.json` describe production systems: keep them on the analyst's machine or the team's own storage.
The prompt files carry aggregated tables only: identifiers inside resource names are masked, but service, host and
dependency names — internal host names and hosts named by IP address included — appear as they do in the exports; no raw
spans and no timestamps below the minute. The notebook, the diagrams and the config carry no credentials.
