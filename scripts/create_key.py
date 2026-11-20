"""Create an API key: python scripts/create_key.py <name> [reader|admin]. Prints the key once; only its hash is stored."""
import secrets
import sys

from urja_api import auth, db
from urja_api.config import Settings

name, role = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "reader")
key = "urja_" + secrets.token_urlsafe(24)
s = Settings()
db.init(s.db_path)
c = db.connect(s.db_path)
c.execute("INSERT INTO api_keys(key_hash,name,role) VALUES (?,?,?)", (auth.hash_key(key), name, role))
c.commit()
print(f"{role} key for {name!r}: {key}")
