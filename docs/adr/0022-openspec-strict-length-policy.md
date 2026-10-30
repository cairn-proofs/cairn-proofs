# ADR-0022: Validate the spec corpus non-strict; keep strict where specs are authored

## Status

Accepted (2026-10-27)

## Context

OpenSpec 1.15.0 changed strict-mode validation to fail on any `WARNING`, not only `ERROR`. The validity rule, in the installed validator, is: `valid = strictMode ? (errors === 0 && warnings === 0) : (errors === 0)`.

One warning is load-bearing here: `REQUIREMENT_TOO_LONG`. The validator emits it when a requirement's text exceeds `MAX_REQUIREMENT_TEXT_LENGTH = 500` characters. That threshold is a hardcoded constant. The validator offers no per-rule severity override, no configurable limit, and no feature flag to demote it. `openspec config` is global and governs workflow/delivery/telemetry only.

The Cairn spec corpus has many requirements above 500 characters by deliberate authoring style. Measured across all specs under `--strict`: 105 `WARNING`s, 0 `ERROR`s. Every warning is `REQUIREMENT_TOO_LONG`. No SHALL/MUST-keyword warning and no other strict-only finding fires anywhere in the corpus. So in this corpus, `--strict` on `openspec validate --specs` catches exactly one thing non-strict does not: the length rule.

Splitting 105 over-length requirements is real work that also touches requirement IDs, so it cannot land immediately. Meanwhile, `mise run check`'s `spec-validate` task and the `openspec-validate-specs` pre-commit hook both ran `openspec validate --specs --strict`. So the whole corpus failed the pre-merge gate the moment the toolchain moved to 1.15.0.

The team keeps OpenSpec at 1.15.0 (pinned in `mise.toml`). Pinning back is not the chosen path.

## Decision

Validate the committed spec **corpus** non-strict, and keep `--strict` everywhere a **new or edited** spec is authored.

1. The corpus gates drop `--strict`. The `spec-validate` task in `mise.toml` and the `openspec-validate-specs` pre-commit hook run `openspec validate --specs`. Non-strict keeps every `ERROR`-level check (document structure, the 4-hashtag scenario format, reference resolution). It passes the corpus clean (0 failed).

2. Change-level validation stays strict. The propose, apply, and bugfix flows run `openspec validate <change> --strict` on an in-flight change's delta specs, so a newly authored requirement over 500 characters fails at authoring time. The length rule is enforced where new content enters, not retroactively against the existing corpus.

3. The separate `spec-ids` task (`openspec/tools/spec_ids.py`) continues to enforce the project's identifier rules, independent of OpenSpec strict mode. The rules are presence, uniqueness, no-vanish, no-reuse, reference resolution, prefix-matches-capability, and scenario binding.

The existing long requirements are fixed opportunistically. Each change that modifies a long requirement splits it as part of that change's delta, and that delta passes through the strict change-level gate. The corpus shrinks its warning count over time, and no dedicated mass-rewrite blocks the gate now.

Alternatives considered:

1. **Pin OpenSpec below 1.15.0.** Rejected. It freezes the toolchain off 1.15.0 to avoid one promoted warning and reconciles against the already-committed 1.15.0 pin. The team chose to stay current.
2. **Drop `--strict` everywhere, including change-level validation.** Rejected. It would stop enforcing the length rule on new specs too, losing the forward guard. Strict is cheap to keep where authoring happens.
3. **Split all 105 over-length requirements now.** Rejected as the immediate unblock. It is the right end state, but it is a multi-spec change that touches IDs. It cannot gate the current merge. It proceeds incrementally, per-change.
4. **Configure the rule off.** Not available. The 1.15.0 validator exposes no per-rule severity, threshold, or feature-flag knob for `REQUIREMENT_TOO_LONG`.

This decision changes no Requirement. It adjusts how the toolchain validates specs, which sits below the Requirement layer. So it is recorded as an ADR, not as an OpenSpec change (per the ADR-vs-OpenSpec boundary, ADR-0001).

## Consequences

- `mise run check` and the pre-commit hook pass against the current corpus while OpenSpec stays at 1.15.0.
- A new or edited requirement over 500 characters still fails, at change-authoring time, through the strict change-level gate. The forward guard is intact.
- The corpus gate no longer flags a pre-existing long requirement. The `spec-ids` task and the non-strict `ERROR`-level checks remain the corpus's structural enforcement.
- The over-length requirements are technical debt fixed opportunistically per change, tracked as future work. Restoring `--strict` on the corpus gates is the exit condition once the count reaches zero.
- `toolchain/spec.md` describes `mise run check` as running strict spec validation. That wording is reconciled when the toolchain spec is next amended through a change, consistent with the opportunistic-fix policy above. This ADR is the interim record.

## References

- `mise.toml` — the `spec-validate` task (now non-strict, with the policy comment).
- `.pre-commit-config.yaml` — the `openspec-validate-specs` hook (now non-strict).
- `.kiro/skills/openspec-propose`, `openspec-apply-change`, `.kiro/skills/bugfix/SKILL.md` — change-level `openspec validate <change> --strict`, which stays strict.
- `openspec/tools/spec_ids.py` — the identifier linter that enforces the project's ID rules independent of OpenSpec strict mode.
- The over-length-requirement split follow-up (tracked as future work) — tracks the exit condition for restoring corpus `--strict`.
- ADR-0001 — the ADR-vs-OpenSpec boundary that routes this no-Requirement-change decision to an ADR.
