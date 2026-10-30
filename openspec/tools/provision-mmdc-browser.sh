#!/usr/bin/env bash
# Provision the headless browser that mmdc (mermaid-cli) needs to render diagrams.
#
# `mise install` installs the `mmdc` binary, but the mise npm backend does not run
# Puppeteer's postinstall browser download, and mmdc's headless mode renders via
# the separate `chrome-headless-shell` browser. This one-time, idempotent step
# provisions it so threat-model diagram rendering (openspec/tools/tc_render.py,
# the `threat-model` agent) works.
#
# Deliberately NOT a mise postinstall hook: rendering is dev-time only, so CI and
# deploy paths must not pay a ~158MB browser download. tc_render.py degrades
# gracefully (empty image slots, threat models still validate) when this is
# skipped, so it is safe to not run in headless / CI environments.
#
# bash 3.2-compatible (macOS ships 3.2); see AGENTS.md "macOS bash portability".
set -euo pipefail

if ! command -v mmdc >/dev/null 2>&1; then
  echo "provision-mmdc-browser: mmdc not on PATH — run 'mise install' first." >&2
  exit 0
fi

if ! command -v mise >/dev/null 2>&1; then
  echo "provision-mmdc-browser: mise not on PATH; cannot locate mermaid-cli." >&2
  exit 0
fi

mcli_base="$(mise where 'npm:@mermaid-js/mermaid-cli' 2>/dev/null || true)"
mcli="${mcli_base}/lib/node_modules/@mermaid-js/mermaid-cli"
if [ ! -d "${mcli}/node_modules/puppeteer" ]; then
  echo "provision-mmdc-browser: could not locate mermaid-cli's puppeteer under ${mcli}." >&2
  exit 0
fi

# `puppeteer browsers install` is idempotent — a no-op when the matching build is
# already present. Uses Puppeteer's default cache (~/.cache/puppeteer), which is
# the same path mmdc resolves at runtime.
echo "provision-mmdc-browser: ensuring chrome-headless-shell for Puppeteer…"
( cd "${mcli}" && npx puppeteer browsers install chrome-headless-shell )
echo "provision-mmdc-browser: done. 'mise run tm-render <change>' can now embed diagrams."
