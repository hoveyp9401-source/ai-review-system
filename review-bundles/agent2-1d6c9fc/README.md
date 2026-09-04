# Agent2 latest review bundle

This branch is a review-only snapshot for Kimi or another reviewer.

## Status

- Review candidate: `1d6c9fc7b3052c413511603eded236c3810c5027`
- Current production runtime remains: `8fa1720cad5284d6543aefe65a539c85073cf889` (`v76bg`)
- The candidate is **not deployed** and must not be described as stable.
- Targeted local regression: 405 passed.
- Production-server isolated formal-entry replay and full regression are still pending.

## Contents

- `agent2-latest-source-1d6c9fc.tar.gz.part001` through `part010`: sanitized current source snapshot.
- `agent2-latest-changes-8fa1720-to-1d6c9fc.patch`: focused code/test diff from the current production commit to the review candidate.

The source archive contains `app/`, relevant project configuration and ADRs. It intentionally excludes the internal issue ledger, production outputs, backups, temporary recovery scripts, full test corpus, and user original messages.

Archive SHA-256:

`8508179429D45F88B41D1D58FF277FC0799E79E6E9D0F9E5BF2DFDB36E0829EE`

Patch SHA-256:

`B477FFB663BDED6760FEDE8B6B0B5D7A68936FA2E8CB6AC55102AECA19B01DF0`

## Reassemble

Linux/macOS:

```bash
cat agent2-latest-source-1d6c9fc.tar.gz.part* > agent2-latest-source-1d6c9fc.tar.gz
sha256sum agent2-latest-source-1d6c9fc.tar.gz
tar -xzf agent2-latest-source-1d6c9fc.tar.gz
```

PowerShell:

```powershell
$parts = Get-ChildItem agent2-latest-source-1d6c9fc.tar.gz.part* | Sort-Object Name
$out = [System.IO.File]::Create("agent2-latest-source-1d6c9fc.tar.gz")
try {
  foreach ($part in $parts) {
    $bytes = [System.IO.File]::ReadAllBytes($part.FullName)
    $out.Write($bytes, 0, $bytes.Length)
  }
} finally {
  $out.Dispose()
}
Get-FileHash agent2-latest-source-1d6c9fc.tar.gz -Algorithm SHA256
tar -xzf agent2-latest-source-1d6c9fc.tar.gz
```

## Review focus

1. Whether the substantial Daily path has truly reduced repeated semantic interpretation.
2. Whether model-owned Daily field decisions can still be overridden by deterministic title/anchor rules.
3. Whether a complete resend can atomically replace a partial draft without duplicating items.
4. Whether explicit empty fields are retained as first-class facts.
5. Whether a zero-write receipt can ever produce a false success reply.
6. Whether the change introduces regressions in short Daily input, Weekly Work Plans, history queries, or concurrent users.
