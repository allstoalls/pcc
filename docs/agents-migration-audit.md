# AGENTS.md migration audit — 2026-09-08

This is a review artifact, not startup reading. The comparison baseline is
`d87f85940fe8fc6301f428f74e9b89bdfbac061a:AGENTS.md` (1,457 lines; SHA-256
`08cb942d6f9f706074433a7d4e23d2d976068b1cfbe3b93953d9cc599f9c5202`).
The additional 31-line performance audit written before cleanup was uncommitted;
its rules remain in AGENTS.md's compiler-performance section.

Run from the repository root:

```bash
env -u LC_ALL uv run python scripts/check_agent_guide_migration.py
git diff d87f85940fe8fc6301f428f74e9b89bdfbac061a -- AGENTS.md docs/
```

The checker proves section accounting, four verbatim contract moves, tool-table
preservation and local links. It **does not prove that a paraphrase preserves
every implication**, or that an agent will follow the rules. The table and the
explicit changes below are the human-review surface. New untracked destination
files must be read too; ordinary `git diff` does not display their contents.

## Section coverage

Every level-two section in the baseline must appear exactly once here.

| Old section | Disposition | Destination / review decision |
|---|---|---|
| `Read Next` | Changed by user direction | [Start with code](../AGENTS.md#start-with-current-code): source first, targeted history search and revision checks replace mandatory whole-document reading. |
| `Dependency ownership contract (maintainer directive, 2026-09-07)` | Verbatim move | [Dependency contract](compiler-contract.md#dependency-ownership-contract-maintainer-directive-2026-09-07). |
| `CPython library contract (maintainer directive, 2026-09-08)` | Verbatim move | [Library contract](compiler-contract.md#cpython-library-contract-maintainer-directive-2026-09-08). |
| `Public CLI parity contract (maintainer directive, 2026-09-08)` | Verbatim move | [CLI contract](compiler-contract.md#public-cli-parity-contract-maintainer-directive-2026-09-08). |
| `Working agreement` | Merged | [Task intake](../AGENTS.md#start-with-current-code), [shared work](../AGENTS.md#shared-work-and-changes) and [investigation workflow](investigation-workflow.md): issue tracking, dated handoff, honest closure; retired-board narrative removed. |
| `Project Intent (north star — read before changing direction)` | Verbatim move | [Project Intent](project-intent.md): complete 161-line body, with a separate note distinguishing historical implementation observations from standing goals. |
| `Project Summary` | Merged / stale descriptions removed | [Project contracts](../AGENTS.md#project-contracts), [source routing](../AGENTS.md#source-and-documentation-routing) and [focused diagnosis](validation-workflow.md#focused-diagnosis); remove maturity/coverage assertions from startup. |
| `Environment Rules` | Merged | [Validation](../AGENTS.md#validation), [shared work](../AGENTS.md#shared-work-and-changes), [heavy runs](validation-workflow.md#heavy-builds-timing-and-bootstrap): uv/locale, fail-fast, logs, watchdog, source stability, no destructive edits. |
| `Autonomous Convergence Guardrails` | Merged; omissions repaired | [Performance](../AGENTS.md#compiler-performance-prove-the-selected-pipeline-first), [completion](validation-workflow.md#evidence-and-completion), [record-family checks](validation-workflow.md#focused-diagnosis). See the repaired requirements below. |
| `Interrupting tasks` | Merged | [Task intake](../AGENTS.md#start-with-current-code): preserve and resume unfinished parent scope; current user can stop/replace it. |
| `Repository Map` | Replaced with source locators | [Routing](../AGENTS.md#source-and-documentation-routing); paths locate current code, removed prose does not define capability. |
| `Dev Tools — check here before writing a probe` | Table moved verbatim; prose merged | [Tool index](development-tools.md), [profiling checks](development-tools.md#profiling-checks), [shared work](../AGENTS.md#shared-work-and-changes). |
| `Compile Modes` | Implementation descriptions removed | [CLI source routing](../AGENTS.md#source-and-documentation-routing): inspect actual dispatch/parser; no copied defaults or LLVM-link assumptions. |
| `Python Frontend / Bootstrap` | Merged; stale state removed | [Project contracts](../AGENTS.md#project-contracts), [validation](validation-workflow.md), [Intent](project-intent.md). Old LLVM default, package-first priority and host-subprocess exception removed. |
| `Runtime & GC Backends` | Merged | [Compiler contracts](compiler-contract.md), [Intent](project-intent.md), [GC validation](validation-workflow.md#focused-diagnosis): five backends, per-backend gates, baseline preservation, reference reading, migration equality. |
| `Evidence Discipline (read before any measure/verify loop)` | Merged; anecdotes removed | [Performance](../AGENTS.md#compiler-performance-prove-the-selected-pipeline-first), [validation](validation-workflow.md), [profiling](development-tools.md#profiling-checks): matching artifacts, controls, real boundary, source stability and no misleading speed claims. |
| `Debugging Playbook` | Existing destination retained | [Playbook](debugging-playbook.md): stable numbered sections; explicit external-oracle limits and corrected watchdog example. |
| `C Codegen Invariants — Signedness` | Merged / source lookup | [Focused diagnosis](validation-workflow.md#focused-diagnosis), [playbook §10–12](debugging-playbook.md#10-separate-data-layout-bugs-from-expression-semantics-bugs); copied helper inventory removed. |
| `Python Codegen / Runtime Invariants` | Merged / source lookup | [Focused diagnosis](validation-workflow.md#focused-diagnosis): ownership, barriers, errors and layout checks retained; fixed byte offsets and symptom-first guessing order removed. |
| `Common Pitfalls` | Duplicate summary removed | [Source routing](../AGENTS.md#source-and-documentation-routing) and [playbook](debugging-playbook.md); search relevant investigations. |
| `Testing & Definition of Done` | Merged | [Validation](validation-workflow.md): regression, original integration, subsystem checks, native/bootstrap qualification and explicit incomplete gates. |
| `Package / NumPy Claim Hygiene` | Merged | [Intent obligation 3](project-intent.md), [library contract](compiler-contract.md#cpython-library-contract-maintainer-directive-2026-09-08), [project contracts](../AGENTS.md#project-contracts): real install/import/ABI, mode labels, no package shortcuts. |
| `Platform Gotchas (macOS)` | Historical locator removed | [Source routing](../AGENTS.md#source-and-documentation-routing): platform-specific facts belong in current tests and relevant investigations. |
| `IR Fix Policy` | Merged; narrow allowance restored | [Focused diagnosis](validation-workflow.md#focused-diagnosis): semantic fixes at producer, only narrow va_arg postprocessing, explicit optimization is a separate layer; external owner remains migration debt. |
| `Investigation Workflow (mandatory for any non-trivial bug)` | Changed by user direction | [Workflow](investigation-workflow.md): source first, scoped historical checks, preserved observations, dated corrections and reproducible indexes. |
| `Maintainer Notes` | Retained | [Shared work](../AGENTS.md#shared-work-and-changes): no unrelated release-metadata changes; inspect release guidance when that task is requested. |

## Omissions found and repaired in this audit

- Conceptual closure again requires all four proofs for residual diagnostic
  representations: normal-path unreachability, zero-use inventory/counter,
  exact lazy-adapter regression and named evidence.
- A structural migration below 1.05x still needs exact outputs/diagnostics,
  no material CPU/instruction/memory regression and removal of named debt.
- The three-candidate rule again includes the below-10% Amdahl-ceiling branch.
- Restored explicit issue/handoff duties, deletion protection, the narrow
  `postprocess_ir_text()` allowance and zero-use/owner checks for diagnostic
  record adapters. None is waived merely to shorten the entry document.

## Deliberate changes to review

Current user direction replaces blanket history reading with source-first,
targeted investigation. Historical `[DENIED]` markers require prerequisite
checks; they are neither ignored nor an unconditional ban on changed code.
Correcting a resolved investigation may append a dated correction rather than
being forbidden forever. References to code offsets, defaults, old timings and
old installed artifacts were removed as current facts, not erased from history.

The original Intent and the three dated contracts are exact moves, not
paraphrase-based coverage claims. Other rows require semantic review. Existing
keyword/consistency tests are supporting checks, not a certificate of completeness.
