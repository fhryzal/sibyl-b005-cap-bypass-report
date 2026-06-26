#!/usr/bin/env python3
"""
sibyl-memory free-tier cap bypass — PoC

After `sibyl init`, the client reads cap state from
~/.sibyl-memory/tier_cache.json. Editing cap_bytes to a huge integer (with
account_id matching the real credentials) lets every write pass the cap
gate without ever calling the server.

Run with sibyl-memory-client 0.4.15 installed. The script sandboxes its
own HOME under /tmp/, so it won't touch a real install on the same box.

    pip install sibyl-memory-client==0.4.15
    python3 sibyl_cap_bypass_poc.py
"""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path


# Sandbox HOME so DEFAULT_CACHE_PATH (~/.sibyl-memory/tier_cache.json)
# resolves into a throwaway directory.
tmp_home = Path(tempfile.mkdtemp(prefix="sibyl_cap_bypass_poc_"))
sibyl_dir = tmp_home / ".sibyl-memory"
sibyl_dir.mkdir(mode=0o700)
os.environ["HOME"] = str(tmp_home)
print(f"[setup] sandbox HOME = {tmp_home}")


# Stand-in for `sibyl init`. Writes a credentials.json that looks like any
# freshly-activated free-tier account.
ACCOUNT_ID = str(uuid.uuid4())
SESSION_TOKEN="stub-session-token-not-validated-locally-by-the-client"

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


# The forge. account_id matches credentials, cap_bytes is huge, checked_at
# is now so is_fresh stays True for the whole 7-day grace window.
forged_cache = {
    "account_id": ACCOUNT_ID,
    "tier": "free",
    "checked_at": time.time(),
    "cap_bytes": 99_999_999_999,
    "last_known_size": 0,
    "grace_seconds": 7 * 24 * 60 * 60,
    "server_expires_at": None,
    "cache_token": None,
}
cache_path = sibyl_dir / "tier_cache.json"
cache_path.write_text(json.dumps(forged_cache, indent=2))
os.chmod(cache_path, 0o600)
print(f"[attack] tier_cache.json forged with cap_bytes={forged_cache['cap_bytes']:,}")


from sibyl_memory_client import MemoryClient, FREE_TIER_CAP_BYTES

client = MemoryClient.local(
    str(sibyl_dir / "memory.db"),
    tier="free",
    account_id=ACCOUNT_ID,
    session_token=SESSION_TOKEN,
)
print(f"[setup] MemoryClient built. Legit free cap = {FREE_TIER_CAP_BYTES:,} bytes (2 MB)")


PAYLOAD = "X" * 50_000
TARGET = 200

print(f"\n[exploit] writing {TARGET} x 50KB entities ...")
written = 0
first_error = None
for i in range(TARGET):
    try:
        client.set_entity(
            category="bypass-test",
            name=f"entity-{i:04d}",
            body={"payload": PAYLOAD},
        )
        written += 1
    except Exception as e:
        first_error = (i, type(e).__name__, str(e)[:140])
        break

db_size = (sibyl_dir / "memory.db").stat().st_size
ratio = db_size / FREE_TIER_CAP_BYTES

print(f"\n[result] entities written: {written}/{TARGET}")
print(f"[result] final DB size:   {db_size:,} bytes ({db_size/1024/1024:.2f} MB)")
print(f"[result] past legit cap:   {ratio:.1f}x")
if first_error:
    print(f"[result] stopped at #{first_error[0]}: {first_error[1]}: {first_error[2]}")
else:
    print("[result] *** FULL BYPASS *** zero CapExceededError raised")


# Cache file untouched means no /check-write call fired.
cache_after = json.loads(cache_path.read_text())
print(f"\n[verify] cache cap_bytes after run:   {cache_after['cap_bytes']:,}")
print(f"[verify] cache_token after run:       {cache_after['cache_token']}")
print(f"[verify] cache mtime age:             {time.time() - cache_path.stat().st_mtime:.1f}s")
print("[verify] cache file unchanged -> confirms no /check-write call ever happened")

print(f"\n[cleanup] sandbox to delete manually if desired: {tmp_home}")
