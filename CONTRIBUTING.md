# CONTRIBUTING

> All contributions — human or AI-assisted — must conform to `rule.md` and `ARCHITECTURE.md`. This file is the operational handbook.

---

## 1. Ground Rules

1. **Never bypass the phase gates.** Code for Phase N+1 may not land until Phase N is approved in `CHANGELOG.md`.
2. **Never invent dependencies.** Stick to the technology stack listed in `rule.md`. New libraries require an ADR (either as a new file in `docs/decisions/` per the convention introduced in ADR-0030, or appended to `DECISIONS.md` for compatibility with ADR-0001 through ADR-0029).
3. **Never produce placeholder code** (`TODO`, `pass`, `# implement later`, stubbed returns). Every merged module must be production-ready.
4. **Never hardcode model or provider names** outside the registry. Use `ModelRegistry.get(...)` and `PluginRegistry.get(...)`.
5. **Never reach into another bounded context's internals.** Cross-context communication happens via use cases or the Event Bus.

---

## 2. Repository Conventions

### Branching
- `main` — always green; release branch.
- `phase/<n>-<short-name>` — work for the current phase.
- `feature/<context>-<short-name>` — for individual features inside a phase.
- `fix/<short-name>` — bug fixes.
- No long-lived branches besides `main`.

### Commits (Conventional Commits)
- `feat(projects): add version restore use case`
- `fix(rendering): clamp clip end-time to track length`
- `refactor(ai/providers): extract retry policy`
- `docs(architecture): add ADR-0021`
- `chore(deps): bump fastapi to 0.115.x`

### Pull Requests
- One PR per logical unit. PRs that touch > 600 changed LOC require splitting.
- Title follows Conventional Commits.
- Description must include: linked ADR(s), linked issue(s), test plan, screenshots for UI.
- **All CI checks must be green before review.** This is the 10-stage **CI Quality Gate** (ADR-0028); see `CI_QUALITY_GATE.md` for the full spec.

### Running the CI Gate Locally

Before pushing, run the same 10 stages CI will run:

```powershell
# Windows
cd ai creation/backend
.\scripts\run_ci_gate.ps1                 # full gate (needs DATABASE_URL)
.\scripts\run_ci_gate.ps1 -Stages "1-4,10"  # fast pre-push slice (no DB needed)
```

```bash
# macOS / Linux
cd ai\ creation/backend
python scripts/ci_gate.py                 # full gate
python scripts/ci_gate.py --stages 1-4,10 # fast pre-push slice
```

Stages 5–9 require a live pgvector-enabled Postgres. Supply `DATABASE_URL` in the shell or create `backend/.env.validation` (git-ignored) with `DATABASE_URL=postgresql+psycopg://…`. Without it those stages are skipped (reported as such), not failed — so iteration without a DB is fine.

---

## 3. Coding Standards

### Backend (Python)

- Python 3.12+. `ruff` (lint + format), `mypy --strict`, `bandit` (security), `import-linter` (architecture rules).
- Type hints **everywhere**. Domain code must be fully typed and dataclass-based (or `attrs`).
- No I/O in `app/domain/`. Repositories live in `app/infrastructure/db/repositories/`.
- Pydantic v2 for request/response DTOs. Domain entities are **not** Pydantic models.
- Async-first (`async def`) at the API and use-case layer; sync code only inside CPU-bound helpers.
- Errors are explicit: domain raises domain exceptions; the API layer maps them to HTTP status codes in `core/errors.py`.
- Logging via `structlog` JSON formatter; never `print`.
- Secrets via `pydantic-settings`. Never read `os.environ` directly outside `core/config.py`.

### Frontend (TypeScript / Next.js)

- TypeScript `strict: true` always.
- ESLint + Prettier. No `any`; use `unknown` + narrowing.
- Server Components by default; mark client islands with `'use client'`.
- Data fetching via React Query (`@tanstack/react-query`); never `useEffect(fetch)`.
- Forms via React Hook Form + Zod resolver.
- Styling via Tailwind + ShadCN; no inline styles.
- One feature folder per bounded context (`features/<name>/`) with `components/`, `hooks/`, `api/`, `store/`, `schemas/`.

### Database

- All tables: `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`, `created_at`, `updated_at`, `deleted_at` (nullable).
- Soft delete is the default; hard delete requires explicit ADR.
- Foreign keys declared with explicit `ON DELETE` policy.
- Indexes added in the same migration that adds the query path.
- Migrations are forward-only; rollbacks via compensating migrations.

---

## 4. Architectural Rules (Enforced in CI)

`import-linter` enforces:

1. `app/domain/` may not import from `app/application`, `app/api`, `app/infrastructure`, `app/ai`, or any third-party SDK.
2. `app/application/` may not import from `app/api` or `app/infrastructure` (only from `app/application/interfaces` ABCs).
3. `app/api/` may not import from `app/infrastructure/` directly — only via DI.
4. Inside `app/ai/`: `agents` → may use `providers`, `prompts`, `memory`, `tools`. `chains` → may use `providers`, `prompts`. `workflows` → may use everything else. `providers` is a leaf.
5. Provider plugins (`app/ai/providers/*`) may not import from `app/api`, `app/application/use_cases`, or `app/domain/<other contexts>`.
6. The Usage Recorder middleware is the **only** caller of provider plugins from outside the `ai/` package.

A `pyproject.toml` `[tool.importlinter]` block declares these contracts. Phase 2+ work is rejected if linter fails.

---

## 5. Testing Policy

- Domain layer: 100% line coverage; pure unit tests with no fixtures beyond the entity factories.
- Application layer: unit tests with fake repositories and fake providers.
- Infrastructure: integration tests against a disposable Postgres + Redis container.
- API: contract tests using OpenAPI schemathesis.
- UI: Playwright e2e for the critical-path flows (sign up → create project → run pipeline → export).
- Performance: k6 scenarios for the top-10 endpoints + render throughput.
- A PR cannot reduce overall coverage; a PR that lowers domain-layer coverage below 100% is auto-rejected.

---

## 6. Documentation Policy

For every PR that changes architecture:

- Update the relevant section of `ARCHITECTURE.md`.
- Add an ADR for any non-trivial trade-off (see `docs/decisions/` for the file-per-ADR convention introduced in **ADR-0030**, Phase 3 W1.1; older ADRs live inline in `DECISIONS.md` and are not being retro-migrated). Cross-link new file-based ADRs from `DECISIONS.md` with a one-line entry (title + status + relative link) so single-file readers still discover them.
- Add an entry to `CHANGELOG.md` (in `[Unreleased]`).
- If an endpoint changes: update `API_CONTRACT.md` **first**, then implement.

**ADR convention change.** ADR-0030 (`docs/decisions/ADR-0030-export-jobs-partial-unique.md`) is the record of the switch from "ADRs are inline sections in `DECISIONS.md`" to "ADRs are standalone files under `docs/decisions/`, cross-linked from `DECISIONS.md`." See ADR-0030 §Context for the rationale and the migration policy for older ADRs.

**ADRs vs Runbooks (v0.3.3-infra).** ADRs explain **why** an architectural decision was made — context, alternatives, consequences. Runbooks explain **how** a repeatable engineering procedure is executed — step lists, commands, recovery actions. New ADRs should reference the relevant runbook in `docs/engineering/` rather than duplicating its steps; new runbooks should reference the relevant ADR(s) for justification. The first runbook (`docs/engineering/RUNBOOK_WAVE.md`) codifies the Phase 3 Wave process that W1.1–W1.3 followed by hand and that W1.4 onwards executes against directly. The Wave 1.4 ADR (ADR-0033) is the first ADR to reference the runbook in place of inlining operational steps (merged as part of `v0.3.4-phase3-w1.4`, 2026-06-30).

---

## 7. Security Checklist (per PR)

- [ ] No secrets in code or tests.
- [ ] Input validated (Pydantic on backend, Zod on frontend).
- [ ] Authorization checked at the use-case boundary, not the route.
- [ ] User-supplied paths/filenames sanitised before storage.
- [ ] Logs scrub PII.
- [ ] New dependencies vetted (license, maintenance, vulnerabilities).

---

## 8. Working with AI Coding Assistants

This project explicitly anticipates AI assistance. To prevent hallucination:

1. Read `rule.md` and `ARCHITECTURE.md` first.
2. Never invent file paths or module names — consult `ARCHITECTURE.md` §4 / §5.
3. Never reference an SDK signature you have not verified.
4. If `ARCHITECTURE.md` is silent on a detail, **ask** before writing — do not guess.
5. Cite the section of `ARCHITECTURE.md` that motivates any non-trivial change.
6. Reject your own output if it contains `TODO`, `pass`, or `# implement later` — rewrite until it is production-ready.

---

## 9. Definition of Done

A unit of work is "done" when:

- Code passes all CI checks (lint, type-check, security, import-linter, tests).
- Documentation is updated (`ARCHITECTURE.md`, `DECISIONS.md`, `CHANGELOG.md`, `API_CONTRACT.md` as relevant).
- Reviewer has signed off.
- Feature flag (CR-9) controls the new capability if it is user-visible.
- New external calls are routed through the Usage Recorder (CR-12).
- New async work is routed through the priority router (CR-13).
- New AI vendor / model is registered (CR-1 / CR-11) — never hardcoded.
