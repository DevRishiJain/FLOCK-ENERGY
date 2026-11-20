# Reflection

**Assumptions I made.** The biggest one is the timezone. Portal timestamps come back as `dd/mm/yyyy HH:MM` with
no timezone, and since this is a Jaipur utility I treated them as IST and stored UTC. If that guess is wrong,
everything is off by 5h30 — it would be a one-line fix, but it was still a guess. Second, the `kwh`/`kvah`
numbers are cumulative register readings, not per-interval consumption. They always increase and look like an
odometer, so I stored them as-is and only show the difference between readings. Third, the export endpoint is
authoritative — if a meter disappears from it, I delete it on the next sync. And for the hierarchy I decided
that when meters disagree about a node's parent, the most common answer is probably right. That's a heuristic,
not a fact, so I log every disagreement instead of hiding it.

**Most difficult part.** Honestly, getting the first data out at all. The site is a SvelteKit SPA — `curl` on
`/meters` gave me an empty shell, not a table. So I read the compiled JS bundles to see what the frontend was
actually calling, which is how I found the `/portal/*` endpoints. The real find was the "Export all meters"
button: it signs the request with HMAC using a secret the portal hands out at `/portal/keys`. Reproducing that
signature in Python took a couple of tries (the message is `GET\n/portal/export\n<query>\n<timestamp>` — I had
the fields in the wrong order at first). The second hard thing was the hierarchy: each meter carries its own
copy of the path, and they disagree, so a naive tree gives nodes with three parents.

**What I'd do with another day.** An incremental sync — right now it refetches all readings every run, which is
fine for 403 meters but wasteful. Derived per-interval consumption and simple anomaly flags. A proper users
table and key management (create/revoke/expire keys). A contract test that replays recorded portal responses so
a portal change would be caught. And a migration to Postgres once SQLite gets tight.

**A mistake I made.** I assumed the export endpoint paginated like `/portal/meters/search`, so I looped over
pages. It doesn't — it ignores `page` and returns all 403 meters every time, so I fetched ~40k duplicate rows
before noticing. Worse, my first readings crawl had no throttling, hit the portal's 429 limit mid-run, and I
lost 7 meters' readings without noticing until I counted them. Both fixes went into the client: verify count
against `total` instead of trusting pagination, wait on 429, and cap itself at 100 req/min.

**What I'd criticise in my own submission.** The full re-download of readings each sync is the laziest part —
it works, but it's brute force. There is no `changed_at`, so I can't tell a consumer which rows a sync actually
changed. The rate limiter and scheduler are single-process, so this design doesn't survive running two copies.
API keys can't be managed after creation. The map needs internet for OSM tiles. And the portal client tests run
against a fake I wrote from observation — if the real portal changed its behaviour, my tests would still pass.
