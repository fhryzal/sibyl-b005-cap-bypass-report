# Sibyl Memory · B005 Bug Bounty Submission

> Free-tier cap bypass via forged `tier_cache.json` — full PoC, runs end-to-end on a clean install of `sibyl-memory-client` 0.4.15.

**Bounty:** [B005 — Break it or prove it (adversarial)](https://beta.sibyllabs.org/bounties)
**Target:** `sibyl-memory-client` 0.4.15
**Class:** Cap / billing bypass
**Severity:** High — direct billing bypass against the documented 2 MB free-tier limit
**Reward wallet (Base):** `0x9f88119EBc98b3AD0154e99D7888ed5e2b6e060b`

---

## TL;DR

A single edit to `~/.sibyl-memory/tier_cache.json` lets an activated free-tier user write **5×+ past the 2 MB cap** with zero network call, zero exception, and a stable persistence across restarts. The cap gate trusts a forged `cap_bytes` integer verbatim once `account_id` matches.

PoC result (reproducible 100%):

```
[result] entities written: 200/200
[result] final DB size:   10,743,808 bytes (10.25 MB)
[result] past legit cap:   5.1x
[result] *** FULL BYPASS *** zero CapExceededError raised
[verify] cache file unchanged -> confirms no /check-write call ever happened
```

---

## Files

| File | Purpose |
|------|---------|
| [`B005_SUBMISSION.md`](./B005_SUBMISSION.md) | Full formal report — root cause, code references, severity argument, suggested fixes, novelty justification |
| [`sibyl_cap_bypass_poc.py`](./sibyl_cap_bypass_poc.py) | Self-contained PoC. Sandboxes its own `HOME` so it's safe to run alongside a real `~/.sibyl-memory/` install |
| [`poc_run_evidence.txt`](./poc_run_evidence.txt) | Verbatim output from the PoC run shown above |

---

## Reproduce in 30 Seconds

```bash
pip install sibyl-memory-client==0.4.15
curl -O https://raw.githubusercontent.com/fhryzal/sibyl-b005-cap-bypass-report/main/sibyl_cap_bypass_poc.py
python3 sibyl_cap_bypass_poc.py
```

The PoC creates a fresh sandbox under `/tmp/sibyl_cap_bypass_poc_*/` and never touches the operator's real plugin install.

---

## Root Cause (1-paragraph version)

`CapGate.check()` and `_effective_cap_local()` in `sibyl-memory-client/src/sibyl_memory_client/_capcheck.py` trust the local `tier_cache.json` `cap_bytes` field as-is once the cached `account_id` matches the client's `account_id`. The existing **SEC-13** guard only refuses the `(cap_bytes=None, account_id=None)` forge — the case where a forge tries to spoof an *uncapped* paid grant on a never-activated account. The symmetric forge `(cap_bytes=<huge_int>, account_id=<real>)` slips through every gate because the branch at line 376–379 (and the matching `_effective_cap_local` at line 446–453) lacks any local signature check on the cache. The `cache_token` field exists on the entry but is only echoed to the server on subsequent `/check-write` calls — and because the forged cache stays "fresh", the server call never happens.

Full detail, code citations, severity reasoning, and four ranked fix suggestions are in [`B005_SUBMISSION.md`](./B005_SUBMISSION.md).

---

## Novelty

Cross-checked against the three publicly indexed Sibyl reports (CryptoxDylan, SEZAN444, akrailoich) — none of them touched this free-tier numeric-`cap_bytes` forge path. CryptoxDylan explicitly tested the cap on a STAKE-tier account and wrote *"No bypass demonstrated in this tier."* They did not test the activated-free-tier branch where this exploit lives.

---

## Submission Channel

Per [the bounty board](https://beta.sibyllabs.org/bounties):

> *"Discord is the canonical channel. Submit, claim, and verify there."*

This repo is the artifact. The claim is filed via the Sibyl Labs Discord ticket linking to this URL.
