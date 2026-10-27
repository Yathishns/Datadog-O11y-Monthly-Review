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

## spans-2026-11-19.csv — spans

- `duration` <- `Duration`
- `method` <- `Method`
- `resource` <- `Resource`
- `service` <- `Service`
- `status` <- `Status Code`
- `ts` <- `Date`
- unit of `duration`: text (human-readable durations ('1.23 ms'))

Entry-span rule: method or a VERB /path resource present, and the service is neither an integration library nor a host name. Dependency graph method: none.
