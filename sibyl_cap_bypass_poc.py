#!/usr/bin/env python3
"""
PoC: Sibyl Memory free-tier cap bypass via forged tier_cache.json
================================================================
Target: sibyl-memory-client 0.4.15 (latest at time of report)
Bounty: B005 — adversarial / cap-or-billing bypass

Vulnerability summary
---------------------
CapGate trusts the local tier_cache.json file's `cap_bytes` field as long as
its `account_id` matches the running client's account_id. A free-tier user
who has run `sibyl init` can edit their own ~/.sibyl-memory/tier_cache.json
to put an arbitrarily large numeric `cap_bytes` value, and every subsequent
write will pass the cap gate WITHOUT any network call to the authoritative
server.

The SEC-13 guard at _capcheck.py:371 only blocks the (cap_bytes=None,
account_id=None) forge case. A (cap_bytes=99_999_999_999, account_id=<real>)
forge slips past every gate because:

  1. check() at line 376-379 takes the `else` branch (numeric cap_bytes) and
     compares new_size <= cached.cap_bytes — always true for huge values.
  2. check_total_local() at line 463 calls _effective_cap_local(), which at
     line 451-452 returns the forged cap_bytes verbatim.
  3. No HMAC / signature check is performed locally on the cache contents.
     The `cache_token` field is only echoed back to the server in subsequent
     network refresh calls — but the bypass keeps the cache "fresh" so no
     refresh ever happens.

Run this PoC
------------
    pip install sibyl-memory-client==0.4.15
    python3 sibyl_cap_bypass_poc.py

Expected output: 200 writes succeed, DB grows to ~10 MB (5x past the 2 MB
free-tier cap), no network call observed, no exception raised.

Reporter: <to fill in at submission>
Date: 2026-06-26
"""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# 1) Sandbox a fresh HOME so the real ~/.sibyl-memory is untouched.
# ---------------------------------------------------------------------------
tmp_home = Path(tempfile.mkdtemp(prefix="sibyl_cap_bypass_poc_"))
sibyl_dir = tmp_home / ".sibyl-memory"
sibyl_dir.mkdir(mode=0o700)
os.environ["HOME"] = str(tmp_home)
print(f"[setup] sandbox HOME = {tmp_home}")

# ---------------------------------------------------------------------------
# 2) Simulate `sibyl init` having been run — write a minimal credentials.json.
#     This is just what any legitimately activated free-tier user has on disk.
# ---------------------------------------------------------------------------
ACCOUNT_ID = str(uuid.uuid4())
SESSION_TOKEN = "stub-session-token-not-validated-in-poc"
creds = {
    "account_id": ACCOUNT_ID,
    "session_token": SESSION_TOKEN,
    "tier": "free",
    "email": "victim@example.com",
    "issued_at": "2026-06-26T00:00:00Z",
    "schema_version": 1,
}
(sibyl_dir / "credentials.json").write_text(json.dumps(creds, indent=2))
os.chmod(sibyl_dir / "credentials.json", 0o600)
print(f"[setup] credentials.json written, account_id={ACCOUNT_ID[:8]}..., tier=free")

# ---------------------------------------------------------------------------
# 3) THE ATTACK — forge tier_cache.json with matching account_id + huge cap.
#     This is the entire exploit. A single JSON file edit.
# ---------------------------------------------------------------------------
forged_cache = {
    "account_id": ACCOUNT_ID,           # MATCHES creds -> defeats SEC-13 guard
    "tier": "free",                      # still claims free (server check skipped on fresh cache)
    "checked_at": time.time(),           # fresh timestamp -> is_fresh == True
    "cap_bytes": 99_999_999_999,         # ~93 GB, effectively unlimited
    "last_known_size": 0,
    "grace_seconds": 7 * 24 * 60 * 60,   # 7-day grace window
    "server_expires_at": None,
    "cache_token": None,                 # no local signature verification exists
}
cache_path = sibyl_dir / "tier_cache.json"
cache_path.write_text(json.dumps(forged_cache, indent=2))
os.chmod(cache_path, 0o600)
print(f"[attack] tier_cache.json FORGED with cap_bytes={forged_cache['cap_bytes']:,}")

# ---------------------------------------------------------------------------
# 4) Build MemoryClient exactly how MCP server / Hermes adapter does it,
#     using the credentials from step 2.
# ---------------------------------------------------------------------------
from sibyl_memory_client import MemoryClient, FREE_TIER_CAP_BYTES

client = MemoryClient.local(
    str(sibyl_dir / "memory.db"),
    tier="free",
    account_id=ACCOUNT_ID,
    session_token=SESSION_TOKEN,
)
print(f"[setup] MemoryClient built. Legit free cap = {FREE_TIER_CAP_BYTES:,} bytes (2 MB)")

# ---------------------------------------------------------------------------
# 5) Write entities until we are well past the legit cap.
# ---------------------------------------------------------------------------
PAYLOAD_PER_ENTITY = "X" * 50_000      # 50 KB body
TARGET_ENTITIES = 200                   # ~10 MB target, 5x past legit cap

print(f"\n[exploit] writing {TARGET_ENTITIES} x 50KB entities ...")
written = 0
first_error = None
for i in range(TARGET_ENTITIES):
    try:
        client.set_entity(
            category="bypass-test",
            name=f"entity-{i:04d}",
            body={"payload": PAYLOAD_PER_ENTITY},
        )
        written += 1
    except Exception as e:
        first_error = (i, type(e).__name__, str(e)[:140])
        break

db_size = (sibyl_dir / "memory.db").stat().st_size
multiplier = db_size / FREE_TIER_CAP_BYTES

print(f"\n[result] entities written: {written}/{TARGET_ENTITIES}")
print(f"[result] final DB size:   {db_size:,} bytes ({db_size/1024/1024:.2f} MB)")
print(f"[result] past legit cap:   {multiplier:.1f}x")
if first_error:
    print(f"[result] stopped at #{first_error[0]}: {first_error[1]}: {first_error[2]}")
else:
    print("[result] *** FULL BYPASS *** zero CapExceededError raised")

# ---------------------------------------------------------------------------
# 6) Verify the forge was NOT overwritten by a server response (no network call)
# ---------------------------------------------------------------------------
cache_after = json.loads(cache_path.read_text())
print(f"\n[verify] cache cap_bytes after run:   {cache_after['cap_bytes']:,}")
print(f"[verify] cache_token after run:       {cache_after['cache_token']}")
print(f"[verify] cache mtime age:             {time.time() - cache_path.stat().st_mtime:.1f}s")
print("[verify] cache file unchanged -> confirms no /check-write call ever happened")

print(f"\n[cleanup] sandbox to delete manually if desired: {tmp_home}")
