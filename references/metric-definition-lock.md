# Compiled Metric Definition Lock

`contracts/metric-definitions.lock.json` is the Runtime definition bundle for
the five `primary_v1` metrics. It contains only metric identity, adverse
direction, app/sandbox observation windows, the external definition path, and
source SHA-256. The external knowledge-base prose and SQL never enter model
context or task execution.

Generate the lock from an explicitly selected `taptap-data-analysis` Skill:

```bash
python scripts/compile_metric_definitions.py \
  --skill-root /absolute/path/to/taptap-data-analysis
```

Release CI with the knowledge base mounted must verify drift without rewriting:

```bash
TAPTAP_DATA_ANALYSIS_SKILL_ROOT=/absolute/path/to/taptap-data-analysis \
  python scripts/compile_metric_definitions.py --check
```

Compilation resolves the `商店（移动端）` domain through its manifest and metric
index, requires exactly one definition for every registered metric, validates
the definition's business/technical/SQL fields, derives compact observation
windows from its standard names, and hashes each selected source file. A change
to one of the five definitions, its resolved path, direction, or window changes
the bundle hash; unrelated knowledge-base metrics do not.

Runtime validates the lock's own canonical hash and uses its direction rather
than duplicating direction in `result-schemas.yaml`. Every task state and
authoritative task validation receipt binds the bundle hash. A running task
cannot resume after definition drift; create a new task under the reviewed
bundle instead.
