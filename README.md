# sibyl-b005

Two bypasses of `sibyl-memory-client` 0.4.15 that share one primitive: `~/.sibyl-memory/tier_cache.json` is an unsigned local file that the SDK trusts as the server-authoritative tier source.

1. **Cap bypass.** Set `cap_bytes` to a huge integer in the cache file. Write past the 2 MB free-tier cap with no network call. PoC writes 200 entities, DB at 10.25 MB (5.1x the cap), no exception.
2. **Paid-feature unlock.** Set `tier` to `"lifetime"` in the cache file. `client.learn()` and `client.lint()` (paid-only) run for a free account.

The second one bypasses a fix the team shipped two days before this audit (commit `3824655`, the CORE-10 pre-launch audit at client 0.4.15). The team's own comment in `_effective_tier()` flags the fix as `SAFE-MINIMAL — see FLAG below`. This report shows the gap.

## Reproduce

```
pip install sibyl-memory-client==0.4.15
python3 sibyl_cap_bypass_poc.py        # cap bypass
python3 sibyl_tier_escalate_poc.py     # paid-feature unlock
```

Both scripts sandbox their own `HOME` under `/tmp/`, so they won't interfere with a real install.

## What I saw

Cap bypass:

```
[result] entities written: 200/200
[result] final DB size:   10,661,888 bytes (10.17 MB)
[result] past legit cap:   5.1x
[result] *** FULL BYPASS *** zero CapExceededError raised
[verify] cache file unchanged -> confirms no /check-write call ever happened
```

Paid-feature unlock:

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

## Root cause (short)

`TierCache.load()` reads `tier_cache.json` and only shape-validates the fields. No signature check, no cross-reference against `credentials.json`. The `cache_token` field exists and was clearly meant for this, but it's only forwarded to the server on the next `/check-write` — and a forged cache stays "fresh" for 7 days, so that call never fires.

Two downstream consumers trust the file:
- `CapGate.check()` at `_capcheck.py:362-381` reads `cap_bytes` for the cap gate.
- `MemoryClient._effective_tier()` at `client.py:749-780` reads `tier` for the paid-feature gate.

The SEC-13 guard added earlier covers `(cap_bytes=None, account_id=None)` but not the symmetric numeric shape or the `tier` field at all.

Full writeup with code citations and four ranked fix suggestions is in `B005_SUBMISSION.md`.

## Files

- `B005_SUBMISSION.md` — full report
- `sibyl_cap_bypass_poc.py` — cap bypass reproducer
- `sibyl_tier_escalate_poc.py` — paid-feature unlock reproducer
- `poc_run_evidence.txt`, `poc_run_evidence_tier_escalate.txt` — verbatim PoC output
