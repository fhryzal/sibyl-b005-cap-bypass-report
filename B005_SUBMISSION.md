# B005 — free-tier cap bypass via forged tier_cache.json

Target: `sibyl-memory-client 0.4.15` (latest on PyPI at writing time)
Tested: Python 3.12 on Linux

## What it is

Edit one field in `~/.sibyl-memory/tier_cache.json` and the cap gate stops working. An activated free-tier user can keep writing past the documented 2 MB limit with no network round-trip and no exception.

PoC writes 200 x 50 KB entities, DB ends up at 10.25 MB (5.1x the legit cap), and the forged cache file is still untouched at the end of the run.

## Why this matches B005

The bounty card calls out *"cap or billing bypass"* as one of the eligible classes. This is exactly that, with a reproducible script and a concrete fix path. Local-only effect (a user can only inflate their own cap), but the gate it defeats is the one Sibyl ships to enforce free-to-paid conversion, so the bypass cost is real for the business.

## Reproduce

```
pip install sibyl-memory-client==0.4.15
python3 sibyl_cap_bypass_poc.py
```

The script creates its own sandbox `HOME` under `/tmp/`, so it won't interfere with a real `~/.sibyl-memory/` install on the same host.

Verbatim output is in `poc_run_evidence.txt`. Last few lines:

```
[result] entities written: 200/200
[result] final DB size:   10,743,808 bytes (10.25 MB)
[result] past legit cap:   5.1x
[result] *** FULL BYPASS *** zero CapExceededError raised
[verify] cache cap_bytes after run:   99,999,999,999
[verify] cache_token after run:       None
[verify] cache mtime age:             2.1s
[verify] cache file unchanged -> confirms no /check-write call ever happened
```

## Root cause

Two places in `sibyl-memory-client/src/sibyl_memory_client/_capcheck.py` read `cached.cap_bytes` straight off disk and trust it once `account_id` matches.

### `CapGate.check()`, line 362–381

```python
cached = self._cache.load()
if cached and cached.is_fresh and cached.account_id == self.account_id:
    if cached.cap_bytes is None:
        if self.account_id is not None:        # SEC-13 guard
            return
    else:
        # Cached as free with a cap. Enforce locally.
        new_size = self._db_size_fn() + proposed_delta_bytes
        if new_size <= cached.cap_bytes:        # <-- trusts forged value
            return
        return self._refresh_and_check(proposed_delta_bytes)
```

The `else` branch compares against `cached.cap_bytes` directly. With a forged huge value the comparison passes for any realistic DB size, so the server refresh never gets called.

### `_effective_cap_local()`, line 446–453

```python
cached = self._cache.load()
if (cached and cached.account_id == self.account_id
        and self.account_id is not None):
    if cached.cap_bytes is None:
        return None
    if cached.is_fresh:
        return cached.cap_bytes                 # <-- forged value echoed
return self._cap
```

This one feeds `check_total_local()`, which is the gate that runs inside the BEGIN IMMEDIATE write lock. Same blind trust, just on the post-write-size check path.

### What was supposed to catch this

`TierCacheEntry` carries a `cache_token` field, set to `credentials.signature` when the server issues a cache entry (line 656 in `_refresh_and_check`). The comment around it explicitly says it's a defense-in-depth binding between the cache and credentials.

But: it's only sent back to the server on the next `/check-write` call. There's no local verification of `cache_token` against `credentials.signature` in `TierCache.load()` or in `CapGate.check()`. The forge keeps the cache "fresh" via `checked_at = now()`, so `is_fresh` is True for the full 7-day grace window. While fresh, the server call never fires.

### SEC-13 scope

The SEC-13 guard at line 371 was added specifically because of forgery concerns. Comment:

```
# Cached as paid (uncapped) within grace window: allow — but
# ONLY for a real account. A free/pre-activation user has
# account_id=None; a forged tier_cache.json with
# account_id:null + cap_bytes:null matches that null state and
# would otherwise spoof an uncapped account (SEC-13).
```

So the team already considered forgery and added a guard. The guard handles one shape: `(cap_bytes=None, account_id=None)`. The symmetric shape that this report exploits, `(cap_bytes=<huge int>, account_id=<real>)`, slips through because:

- `account_id` matches (it's the user's real account from their own `credentials.json`)
- `cap_bytes is None` is false (it's an int), so the SEC-13 branch doesn't apply
- the `else` branch compares numerically and trusts the value

## Severity argument

Marking this High in my own reckoning, with the understanding that severity is the team's call.

- It's a direct bypass of the gate Sibyl ships to enforce a documented commercial limit.
- No binary patching, no MITM, no credential forging. One JSON file the user already owns.
- Silent. No exception, no telemetry trigger, no UI change. `sibyl status` will happily display the inflated cap (since it reads the same cache).
- Stable across restarts. The forge persists until the cache TTL expires or someone invalidates it.
- Same code path is reachable from every write entry point (`set_entity`, `write_event`, `set_state`, `set_reference`) and from the MCP server + Hermes adapter without modification, since they all route through `MemoryClient`.

What keeps it from being Critical in my view: it's local to one machine and one account. The user can only over-cap their own DB. There's no cross-tenant leak and no remote write, so the blast radius is one user's free-tier conversion.

## Fix suggestions

In order of patch size:

**1. Bound the trusted local cap.**  
Treat any cached `cap_bytes` that isn't `None` or exactly `FREE_TIER_CAP_BYTES` as untrusted. Fall through to `_refresh_and_check`. Server-issued free caps are always one of those two values; anything else is, by elimination, either a bespoke server grant (which should re-verify) or a forge. Smallest possible change.

**2. Refuse `account_id`-set entries with null `cache_token` on read.**  
In `TierCache.load()`, return `None` when `entry.account_id is not None and entry.cache_token is None`. Legitimately server-issued entries always populate `cache_token` (line 656). A forged entry typically won't (the attacker doesn't have the user's signature). Doesn't help if the attacker copies `credentials.signature` into the cache, but raises the bar.

**3. Bound by the existing fail-open ceiling.**  
In `_effective_cap_local()`, return `min(cached.cap_bytes, self._cap * FAIL_OPEN_CEILING_MULT)`. The constant already exists (`FAIL_OPEN_CEILING_MULT = 4`). Limits a successful forge to 8 MB instead of arbitrary. Defense in depth.

**4. HMAC the cache locally.**  
Sign `(account_id, tier, cap_bytes, checked_at, server_expires_at)` with a key derived from `credentials.signature` at write time. Verify on every load. Any edit invalidates the cache, forces a server call. This is the only fix that fully closes the local-trust gap.

(1) is the smallest possible patch and covers the demonstrated PoC. (4) is the one that closes the class.

## Novelty check

Cross-referenced against the three currently public Sibyl reports on GitHub:

- `CryptoxDylan/sibyl-memory-bug-bounty-report` — tested cap behavior on a STAKE tier and wrote *"Cap/tier: fast-path for PAID_TIERS (stake here); server is authoritative on boundary. No bypass demonstrated in this tier."* Didn't test the free-tier numeric-cap forge path.
- `SEZAN444/sibyl-labs-adversarial-audit` — black-box analysis of marketing claims and the Postgres schema. Didn't touch `_capcheck.py` or `tier_cache.json`.
- `akrailoich/sibyl-b001-report` — three CLI robustness defects (corrupted credentials.json, atomic-write race, bad `--db` path). None touched the cap gate.

This finding is the activated-free-tier branch of `CapGate.check()` and `_effective_cap_local()`. Not previously filed.

## What this report doesn't cover

- Server-side reconciliation when the cache TTL eventually expires and a real `/check-write` fires. The size discrepancy should be visible to the server at that point. I can't test that without server access.
- Whether the heartbeat reporter (`_heartbeat.py`) surfaces inflated sizes to telemetry. Telemetry is best-effort and gated behind `SIBYL_MEMORY_TELEMETRY`, so it's not a load-bearing detection layer.
- Whether the same forge survives `sibyl upgrade` or other CLI flows that touch the cache file. (`sibyl status` reads it without invalidation; that's confirmed.)

Happy to extend the PoC into any of those if useful.

## Files

| File | What it is |
|------|-----------|
| `B005_SUBMISSION.md` | This file. |
| `sibyl_cap_bypass_poc.py` | Self-contained reproducer. Sandboxes its own `HOME`. |
| `poc_run_evidence.txt` | Verbatim stdout from one PoC run. |
