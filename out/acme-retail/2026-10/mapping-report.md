# How each export was read

## endpoints.csv — endpoints

- `error_rate` <- `Error Rate`
- `monitors` <- `Monitor Counts`
- `p95` <- `P95 Duration`
- `request_count` <- `Request Count`
- `resource` <- `Name`
- `service` <- `Service`
- unit of `error_rate`: fraction (all values within 0..1)
- unit of `p95`: s (Endpoints list P95 Duration is seconds (observed))

## inferred.tsv — inferred

- `error_rate` <- `Error Rate`
- `intg` <- `Intg`
- `p95` <- `p95 Latency (ns)`
- `request_count` <- `Requests`
- `service` <- `Name`
- `svc_type` <- `Type`
- unit of `error_rate`: fraction (all values within 0..1)
- unit of `p95`: ns (unit from the column header)
- unused columns: favorite

## services.tsv — services

- `envs` <- `Envs`
- `error_rate` <- `Error Rate`
- `monitors` <- `Monitors`
- `p95` <- `p95 Latency (ns)`
- `request_count` <- `Requests`
- `service` <- `Name`
- `slos` <- `SLOS`
- `svc_type` <- `Type`
- unit of `error_rate`: fraction (all values within 0..1)
- unit of `p95`: ns (unit from the column header)
- unused columns: favorite, Health, Last Deploy

## spans-2026-10-22.csv — spans

- `duration` <- `Duration`
- `env` <- `Env`
- `host` <- `Host`
- `method` <- `Method`
- `operation` <- `Operation Name`
- `parent_id` <- `Parent ID`
- `resource` <- `Resource`
- `service` <- `Service`
- `span_id` <- `Span ID`
- `status` <- `Status Code`
- `trace_id` <- `Trace ID`
- `ts` <- `Date`
- `type` <- `Type`
- unit of `duration`: text (human-readable durations ('1.23 ms'))

Entry-span rule: a root span, or a span whose parent belongs to another service (integration libraries and host names excluded). Dependency graph method: parent_child.
