# Scratch gateway interoperability

Run the existing mock suite and an opt-in real-response test through the same
canonical wrapper:

```sh
./scripts/test-interop.sh /absolute/path/to/gateway-package <source-sha> <build> <version>
```

Use an isolated test machine or VM with Python, Git, Node, the pinned
`requirements.txt` dependencies and a runnable supported harness CLI for gateway
preflight. The package must be built from the exact gateway source under test.
Confirm its provenance and digest before running; the test checks the running
version, build and source stamp. The runner calls `scripts/test.sh` unchanged.
Without the opt-in package setting, the regular suite skips the real-response test.

The test creates a fresh temporary home and gateway base, selects a loopback port,
starts the packaged gateway, and bootstraps its first user with `add-user`.
It uses the ATC adapter's existing operator-login consumer with `asUser`, never a
device token. All data is seeded with public CLI commands. No existing base,
provider credentials or device pairing is used. The process and temporary data
are removed on exit, including the private boot log. Only version and count
summaries are emitted; raw command output and auth frames stay local.

Coverage includes complete REST and CLI inventories, work-item detail reads,
protocol-2 org-wide feed authentication and subscription, and delivery of newly
created work items. It does not yet establish per-session turn/wake history
interoperability: an empty scratch org has no sessions, and normal session spawn
requires provider setup. Browser behavior, live organizations, throughput,
recovery under load and provider execution remain outside this focused test.
