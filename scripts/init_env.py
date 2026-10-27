"""Generate a local demo configuration; never overwrite existing credentials."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env already exists; left unchanged")
value = (root / ".env.example").read_text()
value = value.replace("replace-with-a-random-admin-token", secrets.token_urlsafe(32))
value = value.replace("replace-with-a-different-napcat-token", secrets.token_urlsafe(32))
value = value.replace("replace-with-a-random-worker-token", secrets.token_urlsafe(48))
value = value.replace("replace-with-a-random-database-password", secrets.token_hex(32))
value = value.replace("replace-with-a-random-echo-state-token", secrets.token_urlsafe(32))
with target.open("x") as stream:
    stream.write(value)
target.chmod(0o600)
print("Created .env with distinct random tokens. Edit QQ IDs and model settings before connecting.")
