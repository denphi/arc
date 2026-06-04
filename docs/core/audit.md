# Audit & reports

*Package-provided observers across the research lifecycle, and the assembled
research report.*

The audit subsystem (`arc/runtime/audit.py`, `arc/contracts/audit.py`) lets a
package observe — and optionally block — the loop at defined phases without
patching core.

## Lifecycle phases (`AUDIT_PHASES`)

```text
goal.received
ideation.before   ideation.after   search.after
planning.after    build.after
validation.before validation.after register.after
execution.before  execution.after
review.after      reflection.after  iteration.after
workflow.error
```

Both the YAML workflow engine and the chat phase path dispatch at these
points, so audits are not UI-specific.

## `AuditDispatcher`

Constructed per workflow (`ResearchWorkflow.audit`). `dispatch(phase, **fields)`:

- resolves the phase's actions in priority order, **excluding actions owned by
  a session-disabled package**;
- runs each `AuditActionContract.audit(event, context) → AuditResult`;
- persists every result to provenance + `memory["audit_results"]`;
- emits to a per-front-end **event sink** (`set_event_sink`) — the chat loop
  bridges to `arc.chat.events`, the UI to its own `EventStream`. **The runtime
  module does not import `arc.chat`.**
- a **blocking** action that returns `fail` raises `AuditBlockedError`,
  aborting the run.

## Reports

`assemble_report(context)` builds a structured report: per-phase audit
results, the ideator candidate pool, run history, and **package-contributed
sections** collected from every registered `ReportSectionContract` (again,
excluding disabled packages). A package adds a section via
`provides.report_sections` — no package name is hard-coded in core. See
{doc}`../packages/audit-and-report`.

## API reference

```{eval-rst}
.. automodule:: arc.contracts.audit
   :members:
   :undoc-members:

.. automodule:: arc.runtime.audit
   :members:
   :undoc-members:
```
