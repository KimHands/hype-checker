"""Result cache on the data volume (design-v4 §3): same reel → saved reply, same tool → saved
official text and alternatives. SQLite from the standard library, one file, mode 600.

Claim checks are never cached: they depend on what each post says.
"""

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REEL_MAX_AGE_S = 7 * 86400
TOOL_MAX_AGE_S = 3 * 86400
TOOL_PARTS = ("official", "alternatives")   # exact keys: identifiers may hold SQL wildcard characters
SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (key TEXT PRIMARY KEY, messages TEXT NOT NULL, case_dir TEXT, saved_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tools (identifier TEXT PRIMARY KEY, payload TEXT NOT NULL, saved_at REAL NOT NULL);
"""


def tool_key(identifier):
    """GitHub owner/repo is case-insensitive; a URL folds only scheme and hostname, everything else
    (userinfo, port, path, query, fragment) stays exactly as given."""
    if not identifier.lower().startswith(("https://", "http://")):
        return identifier.lower()
    parts = urlsplit(identifier)
    userinfo, _, hostport = parts.netloc.rpartition("@")
    if hostport.startswith("["):                       # IPv6 literal: [addr]:port
        host, bracket, port = hostport.partition("]")
        host, colon, port = host + bracket, port[:1], port[1:]
    else:
        host, colon, port = hostport.partition(":")
    netloc = (f"{userinfo}@" if _ else "") + host.lower() + colon + port
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))


class Store:
    def __init__(self, path):
        self.path = Path(path)
        fresh = not self.path.exists()
        self.db = sqlite3.connect(self.path)
        if fresh:
            os.chmod(self.path, 0o600)
        self.db.executescript(SCHEMA)

    def reel_get(self, key, now):
        """(messages, saved_at) when fresh, else None."""
        if not key:
            return None
        row = self.db.execute("SELECT messages, saved_at FROM reels WHERE key = ?", (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row and now - row[1] <= REEL_MAX_AGE_S else None

    def reel_forget(self, key):
        """A '정정' on a reply means its saved copy must not be sent again."""
        with self.db:
            self.db.execute("DELETE FROM reels WHERE key = ?", (key,))

    def reel_put(self, key, messages, case_dir, now):
        if key:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO reels VALUES (?, ?, ?, ?)",
                                (key, json.dumps(messages, ensure_ascii=False), case_dir, now))

    def tool_get(self, identifier, now):
        """{part: value} of the parts still fresh; each part (official, alternatives) keeps its own age."""
        if not identifier:
            return None
        keys = {f"{tool_key(identifier)}#{part}": part for part in TOOL_PARTS}
        rows = self.db.execute(f"SELECT identifier, payload, saved_at FROM tools WHERE identifier IN ({','.join('?' * len(keys))})",
                               list(keys)).fetchall()
        fresh = {keys[key]: json.loads(value) for key, value, saved in rows if now - saved <= TOOL_MAX_AGE_S}
        return fresh or None

    def tool_put(self, identifier, payload, now):
        """Saves only the parts given; the others keep their own saved time."""
        if identifier:
            with self.db:
                for part, value in payload.items():
                    if part not in TOOL_PARTS:
                        raise ValueError(f"unknown tool cache part: {part}")
                    self.db.execute("INSERT OR REPLACE INTO tools VALUES (?, ?, ?)",
                                    (f"{tool_key(identifier)}#{part}", json.dumps(value, ensure_ascii=False), now))

    def prune(self, now):
        with self.db:
            self.db.execute("DELETE FROM reels WHERE saved_at < ?", (now - REEL_MAX_AGE_S,))
            self.db.execute("DELETE FROM tools WHERE saved_at < ?", (now - TOOL_MAX_AGE_S,))
