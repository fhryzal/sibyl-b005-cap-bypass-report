# B005 Submission — Free-Tier Cap Bypass via Forged `tier_cache.json`

**Bounty:** B005 — Break it or prove it (adversarial)
**Target:** `sibyl-memory-client` 0.4.15 (latest at submission)
**Class:** Cap / billing bypass
**Discovered:** 2026-06-26
**Reward wallet (Base):** `0x9f88119EBc98b3AD0154e99D7888ed5e2b6e060b`

---

## Summary

An activated free-tier user can permanently bypass the 2 MB free-tier cap by editing a single field in `~/.sibyl-memory/tier_cache.json`. The cap gate trusts a forged `cap_bytes` value verbatim and never makes the authoritative server `/api/plugin/check-write` call, so the bypass works fully offline and is stable across restarts.

A 200-write PoC grows the local memory database to **10.25 MB (5.1× the legit free cap)** with zero `CapExceededError` raised. The same forge would extend indefinitely; 100 GB has no qualitative difference from 2 MB to the gate.

---

## Severity

**High — actionable.** Direct billing / quota bypass against the documented free-tier limit. Fits B005's explicit example: *"cap or billing bypass"*. Local-only effect (a user can only inflate their own cap), but:

- This is exactly the gate Sibyl ships to enforce paid-tier conversion. A bypass that needs zero binary patching, no MITM, and no credential forging — just one JSON edit — undermines that conversion model in the same way a license-key generator undermines a desktop product.
- The cap gate already has documented defense in depth (SEC-11 symlink refusal, SEC-13 null-account guard, T1-4 server expiry anchor, CAP-4 fail-open ceiling, etc.). This finding shows the matching numeric-`cap_bytes` forge slips through every one of them.
- It's silent: the user keeps writing, the server never sees the call, telemetry doesn't fire, and `sibyl status` shows a happy "free" tier with an inflated cap.

---

## Root Cause

`CapGate.check()` and `_effective_cap_local()` both trust `cached.cap_bytes` as-is once `account_id` matches. The existing SEC-13 guard only refuses `(cap_bytes=None, account_id=None)` — the case where a forge tries to spoof an *uncapped* paid grant on a never-activated account. It does not guard the symmetric forge where `cap_bytes` is a huge **integer** on a real account.

### Relevant code paths

`sibyl-memory-client/src/sibyl_memory_client/_capcheck.py`:

```python
# Lines 362–381 (CapGate.check)
cached = self._cache.load()
if cached and cached.is_fresh and cached.account_id == self.account_id:
    if cached.cap_bytes is None:
        if self.account_id is not None:            # SEC-13: only None-cap guarded
            return
    else:
        # Cached as free with a cap. Enforce locally.
        new_size = self._db_size_fn() + proposed_delta_bytes
        if new_size <= cached.cap_bytes:           # <-- trusts forged cap verbatim
            return
        return self._refresh_and_check(proposed_delta_bytes)
```

```python
# Lines 446–453 (_effective_cap_local — used by check_total_local under the write lock)
cached = self._cache.load()
if (cached and cached.account_id == self.account_id
        and self.account_id is not None):
    if cached.cap_bytes is None:
        return None                                # account-matched uncapped
    if cached.is_fresh:
        return cached.cap_bytes                    # <-- forged cap echoed
return self._cap
```

The `cache_token` field exists on `TierCacheEntry` (line 124) and was designed as a defense-in-depth binding to `credentials.signature`. But it is **only sent to the server in subsequent `/check-write` calls** (line 656). It is never verified locally against `credentials.json`. Since a forged cache stays "fresh" (`checked_at = now()` + 7-day grace), the server call never happens, and `cache_token` never gets cross-checked.

---

## Proof of Concept

Run `sibyl_cap_bypass_poc.py` (attached). It performs the entire attack chain in an isolated `HOME` so the operator's real `~/.sibyl-memory/` is never touched.

### Attack flow (3 steps)

1. **`sibyl init` once.** This writes a normal `credentials.json` with `tier: "free"` and a real `account_id`.
2. **Edit `~/.sibyl-memory/tier_cache.json`** to:
   ```json
   {
     "account_id": "<your real account_id from credentials.json>",
     "tier": "free",
     "checked_at": <now>,
     "cap_bytes": 99999999999,
     "last_known_size": 0,
     "grace_seconds": 604800,
     "server_expires_at": null,
     "cache_token": null
   }
   ```
3. **Write at scale.** Every `set_entity` / `write_event` / `set_state` / `set_reference` call now passes the cap gate without hitting the network.

### Observed result (verbatim PoC output)

```
[setup] sandbox HOME = /tmp/sibyl_cap_bypass_poc_pkbgzzul
[setup] credentials.json written, account_id=6e3ba027..., tier=free
[attack] tier_cache.json FORGED with cap_bytes=99,999,999,999
[setup] MemoryClient built. Legit free cap = 2,097,152 bytes (2 MB)

[exploit] writing 200 x 50KB entities ...

[result] entities written: 200/200
[result] final DB size:   10,743,808 bytes (10.25 MB)
[result] past legit cap:   5.1x
[result] *** FULL BYPASS *** zero CapExceededError raised

[verify] cache cap_bytes after run:   99,999,999,999
[verify] cache_token after run:       None
[verify] cache mtime age:             2.1s
[verify] cache file unchanged -> confirms no /check-write call ever happened
```

Reproducible 100% on `sibyl-memory-client 0.4.15` / Python 3.12 / Linux. No external dependencies beyond the published PyPI package.

---

## Why it's a real break (not just "a user editing their own files")

The cap gate was built with a clear adversarial model. The module docstring says:

> *"The slow path … hits the server endpoint POST /api/plugin/check-write … The server is the authoritative source for tier: credentials.json tampering is detected here because the server looks up the real tier from the server-side account database."*

And the SEC-13 fix existed specifically because:

> *"a forged tier_cache.json with account_id:null + cap_bytes:null matches that null state and would otherwise spoof an uncapped account"*

So Sibyl already treats `tier_cache.json` as untrusted input. The fix landed for one shape of forge (None / null-account) and missed the symmetric one (huge integer / real-account). The result is a forge that passes every gate, never triggers the server check, and lets a free-tier user write indefinitely past the documented cap.

---

## Suggested Fixes (in order of cost)

1. **Sign the cache locally.** When `_refresh_and_check` writes the cache, also write an HMAC of `(account_id, tier, cap_bytes, checked_at, server_expires_at)` using a key derived from `credentials.signature`. On every cache read, recompute and reject mismatches. This makes any local edit invalidate the cache, forcing a server call.

2. **Require server affirmation for any non-default `cap_bytes`.** The free-tier default is a constant (`FREE_TIER_CAP_BYTES`). A cached `cap_bytes` that is neither `None` (paid) nor `FREE_TIER_CAP_BYTES` (free default) is by definition a server-issued bespoke cap; treat unsigned bespoke values as untrusted and fall through to `_refresh_and_check`.

3. **Cross-check at read time.** In `TierCache.load()` and `_effective_cap_local()`, refuse any entry whose `account_id` is non-null AND whose `cache_token` is null. The server-issued path always populates `cache_token = credentials_signature` (line 656). A real cache will always have it; a forge typically won't.

4. **Short-term defense in depth.** Reject the bypass at the SQLite layer too: `check_total_local()` could compute `min(cached.cap_bytes, FREE_TIER_CAP_BYTES * FAIL_OPEN_CEILING_MULT)` so a forged huge cap is bounded by the existing fail-open ceiling. This costs nothing to ship and limits damage even if (1)–(3) lag.

(1) is the only fix that fully closes the local-trust gap; (3) is the cheapest immediate stopgap.

---

## Novelty — Not Covered by Existing Reports

Checked against the three publicly indexed Sibyl reports (CryptoxDylan, SEZAN444, akrailoich):

- **CryptoxDylan** (`sibyl-memory-bug-bounty-report`) tested cap behavior on a STAKE tier account and concluded *"Cap/tier: fast-path for PAID_TIERS (stake here); server is authoritative on boundary. No bypass demonstrated in this tier."* They did not test the free-tier forge path.
- **SEZAN444** (`sibyl-labs-adversarial-audit`) was black-box (no plugin access) and analyzed marketing claims + the Postgres schema. They did not touch `_capcheck.py` or `tier_cache.json`.
- **akrailoich** (`sibyl-b001-report`) filed three CLI robustness defects under B001 (corrupted credentials.json crash, atomic-write race, bad `--db` path). None touched the cap gate.

This finding is the free-tier numeric-`cap_bytes` forge, against the activated-account branch of `CapGate.check()` and `_effective_cap_local()`. Not previously filed.

---

## What I Did Not Test

- Whether the server, on the **next** `/check-write` it eventually receives (e.g. after the bypass cache expires), would detect the size discrepancy and revoke. Out of scope without server access.
- Whether the heartbeat reporter (`_heartbeat.py`) surfaces inflated `last_known_size` to telemetry. Out of scope — telemetry is best-effort and gated behind `SIBYL_MEMORY_TELEMETRY`.

If the team would like me to extend the PoC to demonstrate persistence across a `sibyl status` invocation (it does; status reads the same cache and reports the inflated cap) or across a fresh `MemoryClient` open, happy to add it.

---

## Files Attached

| File | Description |
|------|-------------|
| `sibyl_cap_bypass_poc.py` | Self-contained PoC. Run with `python3 sibyl_cap_bypass_poc.py` after `pip install sibyl-memory-client==0.4.15`. Sandboxes its own HOME — safe on a host that already has `~/.sibyl-memory/`. |
| `B005_SUBMISSION.md` | This report. |

---

## Contact

Reporter handle: *to fill in at Discord claim time*
Wallet (USDC / SIBYL on Base): `0x9f88119EBc98b3AD0154e99D7888ed5e2b6e060b`

Happy to walk the team through the trace under the BEGIN IMMEDIATE write lock if useful — the `_effective_cap_local` path is the subtler one and is where any fix should be regression-tested.
