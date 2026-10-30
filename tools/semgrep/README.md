# Vendored Semgrep rules

This directory holds the **committed** Semgrep rule content that the `mise run semgrep`
SAST gate scans against. The gate runs **offline** via `--config tools/semgrep/rules`
and never resolves the mutable `p/...` registry shorthand at scan time.

This is the load-bearing decision from the `add-sast-security-gates` propose-phase
threat model (Decision D3 / mitigation M-1): resolving `p/security-audit`, `p/secrets`,
`p/python`, `p/typescript` against the **mutable** Semgrep registry at scan time would
make the gate's verdict depend on remote mutable rule state — registry tampering or
ordinary rule drift could silently weaken or empty the rule set so the gate passes
vulnerable code. Vendoring the rule content makes every scan reproducible from the repo
alone, removes the scan-time network dependency, and stops finding-location telemetry
egress.

## Layout

```
tools/semgrep/
├── README.md            ← this file (provenance + refresh procedure)
└── rules/
    ├── security-audit.yaml   ← snapshot of registry pack p/security-audit
    ├── secrets.yaml          ← snapshot of registry pack p/secrets
    ├── python.yaml           ← snapshot of registry pack p/python
    └── typescript.yaml       ← snapshot of registry pack p/typescript
```

`semgrep scan --config tools/semgrep/rules ...` loads every `*.yaml` rule file in
`rules/` recursively.

## Provenance

| File | Upstream source | Vendored |
|---|---|---|
| `rules/security-audit.yaml` | `https://semgrep.dev/c/p/security-audit` | 2026-06-12 |
| `rules/secrets.yaml` | `https://semgrep.dev/c/p/secrets` | 2026-06-12 |
| `rules/python.yaml` | `https://semgrep.dev/c/p/python` | 2026-06-12 |
| `rules/typescript.yaml` | `https://semgrep.dev/c/p/typescript` | 2026-06-12 |

Each file is the fully-expanded rule content that `semgrep --config p/<pack>` would
have fetched on the vendored date — i.e. the exact YAML served by the registry `c/`
(config) endpoint for that pack. The rule files are self-contained: every rule is
inlined, with no remote `extends`/include directives.

**On "version":** the Semgrep registry packs are server-composed and do **not** expose
an immutable version tag or digest in their URL — which is precisely why the threat
model requires vendoring rather than a registry pin. The recorded "version" of this
vendored set is therefore the **vendoring date** above plus this commit's git SHA. If a
future Semgrep release supports content-addressed config pinning (an immutable
version/digest), that is an acceptable equivalent to re-vendoring (see Decision D3).

Pinned scanner: `pipx:semgrep@1.166.0` in `mise.toml`.

## Refresh procedure

Refreshing the rules is a **deliberate, reviewable re-vendor commit** — never an
invisible scan-time pull. To refresh:

1. Re-fetch each pack's expanded content from the registry config endpoint:
   ```sh
   for p in security-audit secrets python typescript; do
     curl -sS "https://semgrep.dev/c/p/$p" -o "tools/semgrep/rules/$p.yaml"
   done
   ```
2. Update the **Vendored** dates in the provenance table above.
3. Run `mise run semgrep` and triage any new findings (fix genuine issues; add a
   narrowly-scoped, justified inline `# nosemgrep: <rule-id> — <reason>` /
   `// nosemgrep: <rule-id> — <reason>` suppression for any false positive).
4. Commit the rule diff + this README update together as one `chore:`/`build:` commit
   so the rule-set change is reviewable in the PR diff (threat-model M-1: the residual
   risk of upstream poisoning is caught by review of this re-vendor diff).

Optionally bump the pinned `pipx:semgrep` version in `mise.toml` in the same refresh.
