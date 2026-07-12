"""Prompts bounded context (Slice α6.1).

Holds the :class:`~app.domain.prompts.prompt.Prompt` aggregate — a
**generation input** (prompt text + kind + optional scene/model link) authored
under a project. Prompts are deliberately outside the versioned editorial
aggregate (project + scenes): they do not participate in optimistic
concurrency, snapshots, restore, or diff. See
``docs/domain/PROMPT_AGGREGATE.md`` and
``docs/decisions/ADR-0036-prompts-generation-inputs.md``.
"""
