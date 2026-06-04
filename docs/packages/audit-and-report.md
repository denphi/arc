# Audit actions, auditors, and report hooks

*Declare package observers across the research lifecycle, and contribute
sections to the final report — without patching core.*

See {doc}`../core/audit` for the runtime mechanism (the dispatcher, the
phases, blocking, the event sinks, report assembly). This page is the
*authoring* side.

An auditor is just an `AuditActionContract` contributed by a package and bound
to one lifecycle phase.

## Declare an audit action

```yaml
provides:
  audit_actions:
    - name: materials_unit_audit
      phase: validation.after      # one of arc.contracts.audit.AUDIT_PHASES
      entrypoint: arc.packages.arc-materials.audit.units:UnitAudit
      blocking: false              # true → a 'fail' result aborts the run
      priority: 50                 # lower runs first
```

```python
from arc.contracts.audit import AuditActionContract, AuditEvent, AuditResult

class UnitAudit(AuditActionContract):
    name, phase, priority = "materials_unit_audit", "validation.after", 50

    async def audit(self, event: AuditEvent, context) -> AuditResult:
        return AuditResult(status="pass", summary="units consistent")
```

The manifest may override the class's declared `phase`/`priority`/`blocking`.

## Declare a report section

```yaml
provides:
  report_sections:
    - name: materials_domain_validity
      section_name: domain_validity
      entrypoint: arc.packages.arc-materials.report.validity:DomainValiditySection
```

```python
from arc.contracts.audit import ReportSectionContract

class DomainValiditySection(ReportSectionContract):
    name, section_name = "materials_domain_validity", "domain_validity"

    def contribute(self, context):
        return {"in_range": True}   # or None to skip
```

`ResearchWorkflow.assemble_report()` collects every registered contributor
(excluding disabled packages) and files each under its `section_name`.
