# KlipperMCP reproducibility and dependency advisories

## Frozen integration boundary

Upstream: <https://github.com/mikehatch/KlipperMCP>

Expected commit (unchanged):
`425e16905c16b6c078028b5063fcb21e0591b190`.

The installer compares the full commit identifier rather than an abbreviated
prefix. A different existing checkout is refused, not fetched/reset/updated.
The Kobra upload patch remains byte-identical to the accepted baseline.

Fresh, previously absent targets use the upstream lockfile and `npm ci`, then
`npm run build`. `npm ci` is deliberately **not** a repair/rerun operation:
it removes existing node_modules, so it must never run on an existing target.
Git commands are bound to their requested target by removing inherited GIT_*
routing/config/tracing variables and disabling global/system config. Effective
local-config conversion drivers (including included config) are detected with a
names-only query before patch inspection/application. Configured clean/smudge/
process filters are refused conservatively, including unused drivers; the helper
does not run them, remove them or rewrite the user's Git config. This protects
existing-install read-only inspection from side effects of `git apply --check`.
No `npm audit fix`, automatic dependency upgrade or lockfile regeneration is
part of installation. Missing/inconsistent lockfiles are errors, not reasons
to fall back to `npm install`.

Existing targets are inspected read-only. They must have the expected full
commit, the already-applied upload patch, dependencies and build artifact.
Missing patch/dependencies/build or detected changes stop the installer without
repairing/deleting anything. Fresh installations record fingerprints for later
read-only comparisons. Legacy installations cannot retroactively acquire a
trusted installed-tree baseline simply by calling their current bytes trusted.
Any legacy reuse warning is an incomplete integrity proof, not a clean bill of
health. Existing code/dependencies/builds are never rebuilt or overwritten by a
normal rerun. A partially failed fresh installation is left for explicit
inspection/recovery; the installer does not erase it on the next run.

## Audit snapshot

**Checked:** 2026-10-03T17:19:44+02:00.
**Host-local tools:** Node v22.23.2, npm 12.0.2.
**Lockfile:** upstream lockfileVersion 3,
SHA256 `4eefdd3d5024b754f80572c2f56318b039bbf64772b7608bbf2782d66db96432`.

The upstream package.json and package-lock.json were copied to an ignored
clone-local fixture and compared bytewise to `git show` at the full pinned
commit. `npm audit --package-lock-only --json --ignore-scripts --logs-max=0`
queried the registry there, with a fixture-local cache. Both manifests remained
byte-identical. Exit 1 is the actual advisory result, not an installation/build
failure. No installed checkout was changed.

The registry reported **8 vulnerable package entries: 5 high, 2 moderate,
1 low, 0 critical**. This counts package entries, not unique CVEs/advisories.
The immutable date-specific evidence is
[the full audit JSON](klippermcp-audit-2026-10-03.json); it includes GHSA URLs,
affected ranges, dependency paths and fix metadata. Advisory data can change
independently of this pinned dependency tree.

| Package in pinned lockfile | Version | Maximum reported severity | Examples / context |
|---|---|---|---|
| `@modelcontextprotocol/sdk` | 1.25.3 | high | GHSA-345p-7cg4-v4c7: shared server/transport reuse, cross-client leak |
| `@hono/node-server` | 1.19.9 | high | Static-path authorization bypass/path traversal; GHSA-wc8c-qw6v-h7f6 |
| `hono` | 4.11.7 | high | Authentication, static files, HTTP middleware, SSR and parser advisories; see complete snapshot |
| `fast-uri` | 3.1.0 | high | URI normalization/authority confusion/SSRF; GHSA-v2hh-gcrm-f6hx |
| `path-to-regexp` | 8.3.0 | high | Regex DoS; GHSA-j3q9-mxjg-w52f and GHSA-27v5-c462-wpq7 |
| `ajv` | 8.17.1 | moderate | `$data` ReDoS; GHSA-2g4f-4pwh-qvx6 |
| `qs` | 6.14.1 | moderate | Parser/stringifier DoS; GHSA-w7fw-mjwx-w883, GHSA-q8mj-m7cp-5q26, GHSA-4mjr-xmp4-gh2g |
| `body-parser` | 2.2.2 | low | Size-enforcement DoS; GHSA-v422-hmwv-36x6 |

These include both the directly used MCP SDK and its transitive packages.
Severity/range information comes from the registry snapshot; this is not a
full exploitability assessment, penetration test or dependency remediation.

## Runtime usage and residual risk

The supplied Hermes `config.yaml` launches `node .../dist/index.js` as a
**stdio MCP subprocess**. The pinned source constructs a StdioServerTransport.
The project does not start a public KlipperMCP HTTP listener, reverse proxy or
HTTP transport. An SDK dependency containing HTTP middleware is not proof that
this deployment exposes an HTTP server, but **stdio alone is not proof that
all advisories are non-exploitable**. Reachability depends on actual source
paths, input handling, SDK behavior and how the integration is deployed.

KlipperMCP is privileged by its capabilities: normal MCP tools can contact
Moonraker and control a printer. Hermes approval policy is not a firmware
interlock or a technical ban on direct MCP invocation. Malicious local code,
compromised dependencies, another client or untrusted MCP requests remain
security concerns even without a public HTTP listener. A future SDK upgrade
needs explicit review, a new lockfile baseline and regression tests; it is
not silently performed here.

The Kobra upload extension pre-checks the filename and sends an upload-only
request. Upload and `control_print(action=start)` remain distinct tools;
an upload must never issue a print-start request. The remote filename check
is not an atomic guarantee against another client's concurrent upload.

## Validation scope

Deterministic tests exercise fresh lockfile use, existing installations/reruns,
modified dependencies, wrong commits, absent/already-applied patches and build
failures without contacting a real printer. A separate host-local integration
successfully exercised a copied pinned checkout with real npm/TypeScript and stdio
`initialize`, `notifications/initialized`, `tools/list` only, against a recording
loopback fake Moonraker endpoint. That is not a Clean-VM or hardware test.
The parent independently repeated that integration successfully: npm ci and
build exited 0, the stdio handshake listed 20 tools, the loopback connection
ledger was empty and the real compiled upload/client routine with fake fetch
made no print-start call. The installed source and isolated target remained
byte/mode/mtime-identical across the probes. Raw evidence stays in ignored
`cache/stage3-integration-post-git-fixes/`; it is not published.
The integration test uses an evidence-local npm cache.

The tested host has npm 12.0.2. It warned that esbuild's postinstall was blocked;
the TypeScript build and actual stdio probes still passed. This warning is not
suppressed by enabling arbitrary lifecycle scripts or upgrading dependencies.
Different npm versions/platforms require the separately approved Clean-VM tests.

Doctor never starts an MCP subprocess or runs the build: build-file presence and
read-only source checks are explicitly distinguished from a runtime handshake.
