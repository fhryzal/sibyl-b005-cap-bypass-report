# B005 — unsigned tier_cache.json: cap bypass + paid-feature unlock

Target: `sibyl-memory-client 0.4.15`, `sibyl-memory-cli 0.3.17` (latest on PyPI at writing)
Tested: Python 3.12 on Linux

## Summary

`~/.sibyl-memory/tier_cache.json` is a local 0600 file that the SDK trusts as the server-authoritative tier source within a 7-day grace window. The file is unsigned and only loosely validated, and a single account-matched edit breaks two separate gates that depend on it:

1. **Cap bypass** — set `cap_bytes` to a huge integer, write past the 2 MB free-tier cap. 200 entities written, DB at 10.25 MB (5.1x the legit cap), no exception, no network call.
2. **Paid-feature unlock** — set `tier` to `"lifetime"`, `client.learn()` and `client.lint()` run without raising `TierGateError`. These are advertised paid-tier-only features.

Both are reproduced by self-contained scripts in this repo. The second one is the headline: it bypasses a fix the team shipped two days before this audit (commit `3824655`, the CORE-10 pre-launch audit at `client 0.4.15`).

## Why the second finding is the interesting one

The team's own code comment in `_effective_tier()` (`client.py:749-780`) lays the whole problem out:

> CORE-10 (2026-06-25 pre-launch audit, SAFE-MINIMAL — see FLAG below):
> the paid-feature gates trusted the raw client-supplied `self._tier`,
> so editing credentials.json to `tier:"lifetime"` unlocked the learner
> and linter for free. A full server round-trip on every learn()/lint()
> would be the authoritative fix but risks latency + offline regressions,
> so we use the cheapest server-authoritative signal we already hold: the
> CapGate's tier cache, which is populated by a server-verified
> /check-write boundary call.

So the team:
1. Identified the bug class (`credentials.json` tier edit → free unlock).
2. Decided not to do the authoritative fix (per-call server round-trip).
3. Moved trust from `credentials.json` to `tier_cache.json` and shipped it.
4. Explicitly flagged the fix as `SAFE-MINIMAL — see FLAG below`.

The PoC at `sibyl_tier_escalate_poc.py` shows the fix is bypassed by the same primitive — local file edit — pointed at the new trust anchor. The user keeps `credentials.json` honest at `tier:"free"`, edits `tier_cache.json` to `tier:"lifetime"`, and the paid-feature gate at `_require_paid_tier` allows the call.

The team's own regression test `test_core10_tampered_tier_blocked_when_cache_says_free` only covers one direction: tampered hint + honest cache → deny. The opposite direction — honest hint + tampered cache → allow — is what this report exploits.

## Reproduce

```
pip install sibyl-memory-client==0.4.15
python3 sibyl_cap_bypass_poc.py        # 2 MB cap bypass
python3 sibyl_tier_escalate_poc.py     # paid-feature unlock
```

Both scripts sandbox their own `HOME` under `/tmp/`, so they won't touch a real `~/.sibyl-memory/` install on the same host.

### Cap bypass output (`poc_run_evidence.txt`)

```
[exploit] writing 200 x 50KB entities ...
[result] entities written: 200/200
[result] final DB size:   10,661,888 bytes (10.17 MB)
[result] past legit cap:   5.1x
[result] *** FULL BYPASS *** zero CapExceededError raised
[verify] cache cap_bytes after run:   99,999,999,999
[verify] cache file unchanged -> confirms no /check-write call ever happened
```

### Paid-feature unlock output (`poc_run_evidence_tier_escalate.txt`)

```
[setup] client.get_tier() = 'free'
[setup] client._effective_tier() = 'lifetime'  <- forged cache wins

[exploit] calling client.learn() (paid-only feature)...
[result] learn() SUCCEEDED -- report: LearningRunReport
[!!] PAID FEATURE UNLOCKED for free-tier user

[exploit] calling client.lint() (paid-only feature)...
[result] lint() SUCCEEDED -- report: LintReport
[!!] PAID FEATURE UNLOCKED for free-tier user
```

## Root cause (one paragraph)

`TierCache.load()` in `_capcheck.py:145-170` deserializes `tier_cache.json` and applies only field-shape validation (cast to float, int, etc.). No signature check, no cross-reference against `credentials.json`. The `cache_token` field exists on `TierCacheEntry` and looks like it was meant to bind the cache to a server-issued grant, but it's only ever forwarded to the server on the next `/check-write` call — and a forged cache keeps `is_fresh == True`, so that call never fires. Both downstream consumers (`CapGate.check()` at `_capcheck.py:362-381` and `_effective_cap_local()` at `_capcheck.py:446-453` for the cap; `MemoryClient._effective_tier()` at `client.py:749-780` for the paid-feature gate) read the file's fields verbatim.

### What the existing SEC-13 guard catches and misses

SEC-13 at `_capcheck.py:371`:

```python
if cached.cap_bytes is None:
    if self.account_id is not None:        # SEC-13 guard
        return
```

This refuses the `(cap_bytes=None, account_id=None)` shape: a pre-activation user trying to spoof a paid grant. It does not guard the symmetric numeric shape `(cap_bytes=<huge int>, account_id=<real>)`, which lands in the `else` branch:

```python
else:
    new_size = self._db_size_fn() + proposed_delta_bytes
    if new_size <= cached.cap_bytes:        # trusts forged value
        return
```

That's the cap bypass.

For the paid-feature unlock, `_effective_tier()` reads `cached.tier` directly with no validation against any independent source:

```python
if (cached is not None and cached.is_fresh
        and cached.account_id == account_id
        and account_id is not None):
    return cached.tier
return self._tier
```

The forged cache wins over the honest client hint.

## Severity argument

My read: High for the paid-feature unlock, Medium for the cap bypass. The team's call to make.

**Paid-feature unlock (high):**
- Bypasses a fix the team shipped this week and explicitly markets as the CORE-10 hardening
- Unlocks two advertised paid-tier features (`learn`, `lint`) on a free account
- Direct revenue path: every free user who reads the source can do this
- The team's own comment (`SAFE-MINIMAL — see FLAG below`) acknowledges the fix is partial
- Silent — no telemetry, no UI change, no exception

**Cap bypass (medium):**
- Same primitive, applied to `cap_bytes` instead of `tier`
- Pushes the same gate the team built to enforce free-to-paid conversion
- More plausibly out-of-threat-model (user editing their own files, server eventually reconciles at TTL)
- Bounded by the 7-day cache freshness window before a real `/check-write` fires

What keeps either from being Critical in my view: blast radius is one machine, one account. No remote write, no cross-tenant leak. The user can only over-cap or over-feature their own install.

## Fix suggestions

In order of patch size:

**1. Cross-check `cache_token` on read.**  
In `TierCache.load()`, refuse any entry whose `account_id` is non-null AND `cache_token` is null. Real server-issued entries always populate `cache_token` from `credentials.signature` (`_capcheck.py:656`). Forged entries typically won't. Cheap, but trivially defeated by an attacker who also copies `credentials.signature` into the cache.

**2. Bound the trusted local cap.**  
Treat any cached `cap_bytes` that isn't `None` or exactly `FREE_TIER_CAP_BYTES` as untrusted; fall through to `_refresh_and_check`. Server-issued free caps are always one of those two values. Anything else is either a bespoke server grant (re-verify) or a forge. Closes the cap-bypass path specifically.

**3. Whitelist trusted-cache tier values for paid-feature gates.**  
In `_effective_tier()`, after the cache hit, additionally require `cached.cache_token == self._credentials_signature` (or similar HMAC binding). If the cache wasn't issued in response to a real server call against the user's actual credentials, don't honor `cached.tier`. Closes the paid-feature unlock specifically.

**4. HMAC the cache locally.**  
Sign `(account_id, tier, cap_bytes, checked_at, server_expires_at)` with a key derived from `credentials.signature` at write time, verify on every load. Any edit invalidates the cache, forces a server call. This is the only fix that fully closes the local-trust class and covers both findings plus any future field added to the cache.

(4) is the only one that closes the class; (2) and (3) close one finding each at minimum cost.

## Novelty check

Cross-referenced against the three currently public Sibyl reports on GitHub:

- `CryptoxDylan/sibyl-memory-bug-bounty-report` — tested cap behavior on a STAKE-tier account and wrote *"Cap/tier: fast-path for PAID_TIERS (stake here); server is authoritative on boundary. No bypass demonstrated in this tier."* Did not test the free-tier numeric-`cap_bytes` forge or the `tier` field forge.
- `SEZAN444/sibyl-labs-adversarial-audit` — black-box (no plugin access). Analyzed marketing claims and the Postgres schema. Did not touch `_capcheck.py` or `tier_cache.json`.
- `akrailoich/sibyl-b001-report` — three CLI robustness defects (corrupted `credentials.json` crash, atomic-write race, bad `--db` path). Did not touch the cap gate or the paid-feature gate.

Neither finding here overlaps. The `tier` field forge specifically bypasses the CORE-10 fix that shipped on 2026-06-25 — two days before this report — so it could not have been filed earlier.

## What this report doesn't cover

- Server-side reconciliation when the cache TTL eventually expires and `/check-write` fires. The size or tier discrepancy should be visible to the server at that point. I cannot test that without server access. The forge keeps the cache `is_fresh` for 7 days, so the call doesn't fire on its own.
- Whether the heartbeat reporter (`_heartbeat.py:113`) surfaces inflated `last_known_size` or tier metadata to telemetry. From the source it only sends `account_id` + `event_type: heartbeat` + `heartbeat_count`, not size or tier — so heartbeat does not detect the forge.
- Whether the same forge survives `sibyl upgrade` or other CLI flows that touch the cache. `sibyl status` reads the cache without invalidation; that's confirmed.
- The MCP server build path (`sibyl-memory-mcp/server.py:151-153`) and the Hermes adapter (`sibyl-memory-hermes/provider.py:149-151`) both construct `MemoryClient` through the same `MemoryClient.local()` constructor with the same `account_id` / `session_token` / `tier` plumbing, so the forge propagates through every documented entry point.

## Files

| File | What it is |
|------|-----------|
| `B005_SUBMISSION.md` | This file. |
| `sibyl_cap_bypass_poc.py` | Reproducer for the cap bypass. |
| `sibyl_tier_escalate_poc.py` | Reproducer for the paid-feature unlock. |
| `poc_run_evidence.txt` | Verbatim stdout from one cap-bypass PoC run. |
| `poc_run_evidence_tier_escalate.txt` | Verbatim stdout from one paid-feature unlock PoC run. |
