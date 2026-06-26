#!/usr/bin/env python3
"""
PoC: paid-feature gate bypass via forged tier_cache.json tier field.

The CORE-10 audit (pre-launch 2026-06-25) added _effective_tier() so paid
features no longer trust the client-supplied tier hint or credentials.json.
Instead they trust the cap-gate's tier_cache.json. But the cache itself is
unsigned and editable -- same primitive that broke the cap, just applied to
the tier field instead of cap_bytes.

Forging tier_cache.json with tier="lifetime" + matching account_id unlocks
client.learn() (self-learning) and client.lint() (memory linter) for a free
user, with no server call and no credential edit.
"""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

tmp_home = Path(tempfile.mkdtemp(prefix="sibyl_tier_escalate_poc_"))
sibyl_dir = tmp_home / ".sibyl-memory"
sibyl_dir.mkdir(mode=0o700)
os.environ["HOME"] = str(tmp_home)
print(f"[setup] sandbox HOME = {tmp_home}")

ACCOUNT_ID = str(uuid.uuid4())
TOKEN_STR = "x" * 32  # arbitrary placeholder, never validated locally
creds = {
    "account_id": ACCOUNT_ID,
    "session_token": TOKEN_STR,
    "tier": "free",
    "email": "victim@example.com",
    "issued_at": "2026-06-26T00:00:00Z",
    "schema_version": 1,
}
(sibyl_dir / "credentials.json").write_text(json.dumps(creds, indent=2))
os.chmod(sibyl_dir / "credentials.json", 0o600)
print(f"[setup] credentials.json: tier=free, account_id={ACCOUNT_ID[:8]}...")

forged_cache = {
    "account_id": ACCOUNT_ID,
    "tier": "lifetime",
    "checked_at": time.time(),
    "cap_bytes": None,
    "last_known_size": 0,
    "grace_seconds": 7 * 24 * 60 * 60,
    "server_expires_at": None,
    "cache_token": None,
}
cache_path = sibyl_dir / "tier_cache.json"
cache_path.write_text(json.dumps(forged_cache, indent=2))
os.chmod(cache_path, 0o600)
print(f"[attack] tier_cache.json forged: tier=lifetime, cap_bytes=None")

from sibyl_memory_client import MemoryClient

client = MemoryClient.local(
    str(sibyl_dir / "memory.db"),
    tier="free",
    account_id=ACCOUNT_ID,
    session_token=TOKEN_STR,
)
print(f"[setup] MemoryClient built with tier='free'")
print(f"[setup] client.get_tier() = {client.get_tier()!r}")
print(f"[setup] client._effective_tier() = {client._effective_tier()!r}  <- forged cache wins")

print(f"\n[setup] seeding journal entries for the learner...")
for i in range(5):
    client.write_event(acted=[f"action {i}"], extra={"ctx": f"ctx {i}"})
client.set_entity("project", "demo", {"status": "active"})

print(f"\n[exploit] calling client.learn() (paid-only feature)...")
try:
    report = client.learn()
    print(f"[result] learn() SUCCEEDED -- report: {type(report).__name__}")
    print(f"[!!] PAID FEATURE UNLOCKED for free-tier user")
except Exception as e:
    print(f"[blocked] {type(e).__name__}: {str(e)[:200]}")

print(f"\n[exploit] calling client.lint() (paid-only feature)...")
try:
    lint_report = client.lint()
    print(f"[result] lint() SUCCEEDED -- report: {type(lint_report).__name__}")
    print(f"[!!] PAID FEATURE UNLOCKED for free-tier user")
except Exception as e:
    print(f"[blocked] {type(e).__name__}: {str(e)[:200]}")

cache_after = json.loads(cache_path.read_text())
print(f"\n[verify] cache tier after run:   {cache_after['tier']!r}")
print(f"[verify] cache_token after run:  {cache_after['cache_token']}")
print(f"[verify] cache mtime age:        {time.time() - cache_path.stat().st_mtime:.1f}s")
print(f"[verify] cache file unchanged -> confirms no /check-write call ever happened")
