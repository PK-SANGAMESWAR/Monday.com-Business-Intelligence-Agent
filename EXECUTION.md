# Project Implementation Prompt

Treat `@PROBLEM-STATEMENT.md` as the single source of truth. Build incrementally,
one feature at a time. Do not jump straight to code.

**Workflow per feature:** Understand → Plan → Document → Implement → Test → Debug
→ Verify → Document Results → Mark Complete → Next.

## Phase 1 — Understand

Read `@PROBLEM-STATEMENT.md` fully and extract:

- Functional and non-functional requirements
- User flows, inputs/outputs, required features
- Constraints, tech restrictions, expected APIs
- Data, UI, and edge-case requirements
- Evaluation criteria and deliverables
- Implicit requirements that are technically necessary

No implementation yet.

## Phase 2 — Master Plan

Create `docs/00_IMPLEMENTATION_PLAN.md` covering:

1. Overview and problem understanding
2. Requirements breakdown (functional + non-functional)
3. Architecture, tech stack with justification, folder structure
4. Data models, API design, frontend/backend architecture
5. External dependencies, security, error handling, testing strategy
6. Feature dependency graph, development phases, implementation order
7. Definition of Done and final validation checklist

Implementation order must respect dependencies. Keep it clean and
maintainable — do not over-engineer.

## Phase 3 — Feature-by-Feature

Split the plan into small, independently testable features. For each, create
`docs/features/XX_<FEATURE_NAME>.md` containing, **before** implementation:

1. **Objective** — what the feature accomplishes
2. **Requirement mapping** — cite the requirement and explain how it is satisfied
3. **Technical design** — components, data flow, interfaces, APIs, dependencies,
   state, error handling, edge cases
4. **Files to create/modify** — with responsibilities
5. **Implementation plan** — concrete ordered steps
6. **Test plan** — unit, integration, API, UI, edge-case, failure tests
7. **Acceptance criteria** — objective completion conditions

### Interaction Protocol

- **A. Plan** — present the feature plan; write no implementation code.
- **B. Approve** — wait for my explicit approval.
- **C. Implement** — code only the current feature, nothing future.
- **D. Test** — actually run the tests. Report commands, results, failures,
  errors, warnings, and coverage where useful. Never claim tests "should pass".
- **E. Debug** — analyze root cause, fix, re-run. Do not advance while blocking
  failures remain.
- **F. Document** — update the feature doc with actual implementation, files
  changed, test results, design decisions, known limitations, then mark
  `STATUS: COMPLETE`.

## Testing Philosophy

Test continuously, never only at the end:

```text
Design → Write tests → Implement → Run → Debug → Re-run → Verify → Document
```

Prefer deterministic, reproducible tests. Cover happy path, boundaries, invalid
and empty input, auth, error handling, integration, and regression. When a bug is
found, add a regression test.

## Code Quality

Clear naming, small focused functions, separation of concerns, proper error
handling, type safety, environment-based config, no hardcoded secrets, consistent
formatting, comments only where they add understanding.

No dependency without a reason. No functionality that is not required. No
abstractions that are not earned.

## Documentation Layout

```text
docs/
├── 00_IMPLEMENTATION_PLAN.md
├── FINAL_REQUIREMENT_AUDIT.md
├── FINAL_VALIDATION.md
├── architecture/ARCHITECTURE.md
├── features/01_<FEATURE>.md ...
└── testing/TESTING.md
```

Docs must reflect the actual implementation. Mark status as Planned, In Progress,
Tested, Complete, or Blocked.

## Git

Clean, incremental commits per completed feature (`feat:`, `test:`, `fix:`,
`docs:`). No single giant commit.

## Decision Making

When multiple valid approaches exist: compare them, weigh complexity,
maintainability, reliability, and testability, recommend one, and explain why.
Never change a major architectural decision silently.

Ambiguity in the problem statement must be raised with me before deciding. Use
your own judgment for minor details.

## Verification

After each major feature, confirm it satisfies its requirement in
`PROBLEM-STATEMENT.md`. At project end, produce
`docs/FINAL_REQUIREMENT_AUDIT.md`:

| Requirement | Implementation | Tests | Status |
| ----------- | -------------- | ----- | ------ |
| ...         | ...            | ...   | PASS   |

No requirement is complete without evidence.

Before declaring the project finished, run the full suite, integration and E2E
tests, error-path tests, a clean install and clean startup, plus a documentation
and code-quality review. Record all of it in `docs/FINAL_VALIDATION.md` with
environment, setup, commands, results, coverage, known limitations, and final
status.

## Hard Rules

1. `PROBLEM-STATEMENT.md` is the source of truth.
2. Plan before coding — every feature, every time.
3. One feature at a time; never implement ahead.
4. Write the test plan before the implementation.
5. Execute tests; never assume they pass.
6. Fix failures before moving on.
7. Document actual results, not intentions.
8. Ask for approval before each feature.
9. Never go from problem statement to a large code dump.

## Starting Action

Only do this first:

1. Read `@PROBLEM-STATEMENT.md`.
2. Analyze requirements and list ambiguities or missing information.
3. Propose the architecture.
4. Create `docs/00_IMPLEMENTATION_PLAN.md`.
5. Break the project into ordered, independently testable features and explain
   the ordering.

**No application code yet.** Stop after the master plan and wait for approval
before Feature 01.