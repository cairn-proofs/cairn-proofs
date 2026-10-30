---
inclusion: fileMatch
fileMatchPattern: ["lambda-layers/**", "**/*.sh"]
name: feedback-gpg-agentfree-verification
description: "For GPG signature verification in build scripts on macOS Homebrew, avoid `gpg --import` with a custom GNUPGHOME — it triggers an opaque IPC connect failure. Use the agent-free chain instead."
---

When verifying GPG signatures in a build script that must work cross-platform (macOS Homebrew + Linux), DO NOT use `gpg --import` with a custom `GNUPGHOME` — on macOS Homebrew gnupg this fails with `gpg: can't connect to the gpg-agent: IPC connect call failed` even with `--batch`, because the fresh keyring directory has no agent socket and the auto-launched agent can't bind in the isolated home.

Use the agent-free chain instead:
1. `gpg --batch --with-colons --show-keys <armored-key>` to read fingerprints (data parsing, no agent)
2. `gpg --batch --dearmor --output <keyring.gpg> <armored-key>` to convert to a binary keyring (pure data transform, no agent)
3. `gpgv --keyring <keyring.gpg> --status-fd 1 <sig> <data>` to verify (gpgv has no agent dependency)

**Why:** macOS Homebrew's gnupg starts a fresh agent on first use in a new `GNUPGHOME`, and that agent often fails to bind its IPC socket. The `--show-keys` + `--dearmor` + `gpgv` chain sidesteps the agent entirely. Discovered while building `lambda-layers/notation/build.sh` for the mvp-evidence-store work.

**How to apply:** Any build script doing GPG verification with a project-pinned key. Pair with `--status-fd 1` parsing to assert the signing key's `VALIDSIG <fpr>` matches a pinned fingerprint — otherwise a multi-key keyring lets any contained key sign.
