# Architecture decision records

The plan settles the boilerplate's one-way doors, the choices that are costly to reverse once data or clients depend on them ([architecture §1](../architecture.md#1-tldr-and-principles)). Each new one gets a record here: `NNNN-<slug>.md`, numbered from `0001`, with the sections Status, Context, Decision and Consequences.

Write `0001-tenancy.md` before adding a product's first module ([conventions §4](../conventions.md#4-project-layout)). Tenancy, keeping customers' data apart, is the costliest retrofit ([architecture §6](../architecture.md#6-persistence)). The boilerplate's own tables stay global. A B2B product scopes its tables by `org_id` in one shared schema, under [conventions §6](../conventions.md#6-persistence) Tenancy.
