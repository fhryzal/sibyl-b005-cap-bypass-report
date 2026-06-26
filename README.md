# sibyl-b005

Free-tier cap bypass on `sibyl-memory-client 0.4.15`.

After `sibyl init`, you can edit `~/.sibyl-memory/tier_cache.json` to put any cap you want and the client trusts it. No network call, no exception. Tested writing 200 entities (~10 MB) past the 2 MB free cap with nothing complaining.

## The forge

```json
{
  "account_id": "<your real account_id from credentials.json>",
  "tier": "free",
  "checked_at": <now epoch seconds>,
  "cap_bytes": 99999999999,
  "last_known_size": 0,
  "grace_seconds": 604800,
  "server_expires_at": null,
  "cache_token": null
}
```

That's it. Save the file, keep writing.

## Reproduce

```
pip install sibyl-memory-client==0.4.15
python3 sibyl_cap_bypass_poc.py
```

The script sandboxes its own `HOME` so it won't touch your real install.

## What I saw

```
[exploit] writing 200 x 50KB entities ...
[result] entities written: 200/200
[result] final DB size:   10,743,808 bytes (10.25 MB)
[result] past legit cap:   5.1x
[result] *** FULL BYPASS *** zero CapExceededError raised
[verify] cache file unchanged -> confirms no /check-write call ever happened
```

## Why it works

`CapGate.check()` in `_capcheck.py` around line 376:

```python
else:
    # Cached as free with a cap. Enforce locally.
    new_size = self._db_size_fn() + proposed_delta_bytes
    if new_size <= cached.cap_bytes:
        return
```

`cached.cap_bytes` comes straight off disk. If it's huge, the comparison passes forever and the server never gets called.

The existing SEC-13 guard above this only handles the `(cap_bytes=None, account_id=None)` forge, which is when someone tries to spoof an uncapped paid grant on a never-activated account. The mirror case `(cap_bytes=<big int>, account_id=<real>)` isn't checked.

Same gap in `_effective_cap_local()` around line 446, which is what `check_total_local()` uses inside the BEGIN IMMEDIATE write lock.

The `cache_token` field looks like it was meant to be a tamper binding to `credentials.signature`, but it's only forwarded to the server on the next `/check-write`. Since the forged cache stays fresh, that call never fires.

## Fix ideas, cheapest first

1. Bound the cached cap locally. Treat any cached `cap_bytes` that isn't `None` or `FREE_TIER_CAP_BYTES` as untrusted, fall through to `_refresh_and_check`. Default-server-issued caps for free users are always one of those two values.
2. Cross-check `cache_token`. If `account_id` is set but `cache_token` is null, the entry didn't come from the server. Refuse it on read.
3. HMAC the cache locally. Sign `(account_id, tier, cap_bytes, checked_at, server_expires_at)` with a key derived from `credentials.signature` at write time, verify on load.

(1) is the smallest patch. (3) is the only one that fully closes the local-trust gap.

## Files

- `B005_SUBMISSION.md` — full writeup with code citations and severity argument
- `sibyl_cap_bypass_poc.py` — reproducer
- `poc_run_evidence.txt` — verbatim PoC output

## Not covered

Whether the next eventually-reaching `/check-write` call notices the size discrepancy and revokes. Can't tell without server access. The forge keeps the cache "fresh" for 7 days, so that call doesn't fire on its own — but it would fire if something invalidates the cache or the user crosses the cache TTL.
