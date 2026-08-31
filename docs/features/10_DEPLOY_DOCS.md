# Feature 10 — Deploy & Docs

STATUS: **COMPLETE (docs + deploy artifacts)** — implemented and verified 2026-08-31.
Full offline suite green (382 passed, see `docs/FINAL_VALIDATION.md`). The one item this
feature cannot finish unattended is recorded at the bottom: clicking "Deploy" on Streamlit
Community Cloud requires the repo owner's own account.

## 1. Objective

Close out `docs/00_IMPLEMENTATION_PLAN.md` §7 F10 — the last node in the feature
dependency graph. Every prior feature (F01–F09) is implemented, tested, and green; this
feature does not add agent behaviour, it makes the finished agent **legible and runnable
by someone who was not in this conversation**: a README that explains the architecture and
board setup, a Decision Log capturing what was actually decided and why, the requirement
audit and final validation the plan's Definition of Done requires, and the deploy path
itself.

## 2. Requirement mapping

| ID | Requirement | How this feature satisfies it |
|----|-------------|-------------------------------|
| DL-1 | Hosted prototype, link-accessible, no local setup | `requirements.txt` (uv-exported) + README §Deploy give a repo-owner-executable path to Streamlit Community Cloud. Cannot be completed *by an agent* — see §7. |
| DL-2 | Decision Log, 2 pages max | `docs/DECISION_LOG.md` |
| DL-3 | README with architecture + monday.com setup instructions | `README.md`, rewritten from the one-line stub |
| Plan §10 | `FINAL_REQUIREMENT_AUDIT.md`, `FINAL_VALIDATION.md` | Both added under `docs/` |
| Plan §3.6 layout | `docs/architecture/ARCHITECTURE.md`, `docs/testing/TESTING.md` | Both were empty directories; filled in |

## 3. Technical design

No production code changes. Five new/rewritten documents plus one generated artifact:

- `README.md` — architecture diagram (reused from the plan), local setup, monday.com
  board provisioning (via `scripts/seed_monday.py`, since the boards do not pre-exist),
  running the app, running tests, deploying.
- `docs/DECISION_LOG.md` — assumptions, trade-offs, what would change with more time, and
  the "leadership updates" interpretation, condensed to 2 pages from the much longer
  reasoning already recorded in `00_IMPLEMENTATION_PLAN.md`.
- `docs/architecture/ARCHITECTURE.md` — the layered diagram plus a paragraph per layer,
  extracted and slightly expanded from plan §3.
- `docs/testing/TESTING.md` — how to run the suite, what each test tier covers, current
  coverage numbers, and the one deliberately-skipped tier (`tests/live/`, needs real
  credentials).
- `docs/FINAL_REQUIREMENT_AUDIT.md` — every FR/DL/NFR from plan §2, mapped to the file and
  test that proves it, marked PASS.
- `docs/FINAL_VALIDATION.md` — actual `uv run pytest` output, coverage, environment,
  known limitations, executed on 2026-08-31.
- `requirements.txt` — `uv export --no-dev --no-hashes --format requirements-txt`.
  Streamlit Community Cloud can install from `pyproject.toml` directly via uv, but a
  plain `requirements.txt` is the lowest-common-denominator path and costs nothing to
  keep in sync (regenerate with the same command after any dependency change).

## 4. Files created/modified

| File | Responsibility |
|------|-----------------|
| `README.md` | Rewritten: architecture, setup, board provisioning, run, test, deploy |
| `docs/DECISION_LOG.md` | New — the required deliverable |
| `docs/architecture/ARCHITECTURE.md` | New |
| `docs/testing/TESTING.md` | New |
| `docs/FINAL_REQUIREMENT_AUDIT.md` | New |
| `docs/FINAL_VALIDATION.md` | New |
| `requirements.txt` | New, generated |
| `docs/features/10_DEPLOY_DOCS.md` | This file |

## 5. Implementation plan

1. Run the full offline suite with coverage to get real numbers for the audit/validation
   docs — never write "should pass".
2. Write `docs/architecture/ARCHITECTURE.md` and `docs/testing/TESTING.md` from the
   plan's existing (already-verified) content, so nothing here is a new, untested claim.
3. Write `docs/DECISION_LOG.md`, budget-checked at 2 pages.
4. Write `README.md`.
5. Write `docs/FINAL_REQUIREMENT_AUDIT.md` against plan §2's requirement tables.
6. Write `docs/FINAL_VALIDATION.md` with the §1 test output pasted in verbatim.
7. Generate `requirements.txt`.
8. Mark this doc COMPLETE.

## 6. Test plan

Documentation has no unit tests. The verification is: (a) the full suite actually runs
and its real output is what gets pasted into `FINAL_VALIDATION.md`, and (b) every claim in
`FINAL_REQUIREMENT_AUDIT.md` cites a file that exists and, where applicable, a test that
exists and passes.

## 7. What this feature does not (and cannot) do

- **Click "New app" on share.streamlit.io.** That requires signing in with the repo
  owner's own GitHub identity and pasting `MONDAY_API_KEY` / `ANTHROPIC_API_KEY` into
  Streamlit's secrets UI — an account-bound, credential-bound action outside this
  session's reach. README §Deploy gives the exact steps; DL-1 is otherwise ready
  (repo is public on `origin`, `requirements.txt` present, `app.py` at repo root, secrets
  never touch the repo).
- **Push this commit to `origin`.** Left to the user, consistent with not auto-pushing.

## Acceptance criteria

- [x] Full offline suite green, output captured verbatim in `FINAL_VALIDATION.md`.
- [x] `README.md` is no longer the one-line stub; covers architecture + board setup.
- [x] `docs/DECISION_LOG.md` exists and is ≤2 pages.
- [x] `docs/FINAL_REQUIREMENT_AUDIT.md` covers every FR/DL/NFR row from the plan.
- [x] `docs/architecture/ARCHITECTURE.md` and `docs/testing/TESTING.md` are no longer
      empty directories.
- [ ] App reachable at a public Streamlit URL — **blocked on the repo owner's Streamlit
      account**, not on anything this feature could implement.
