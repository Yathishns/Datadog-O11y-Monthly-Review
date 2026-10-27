# How each export was read

## spans-2026-11-19.csv — spans

- `duration` <- `Duration`
- `method` <- `Method`
- `resource` <- `Resource`
- `service` <- `Service`
- `status` <- `Status Code`
- `ts` <- `Date`
- unit of `duration`: text (human-readable durations ('1.23 ms'))

Entry-span rule: method or a VERB /path resource present, and the service is neither an integration library nor a host name. Dependency graph method: none.
