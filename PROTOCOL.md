# How the Urja Meter Ops portal actually works

Everything below was found with `curl` + reading the portal's JS bundles (the UI is a SvelteKit SPA, so the
HTML is mostly empty and the data comes from XHR calls whose paths are visible in `/_app/immutable/nodes/*.js`).
The portal was used read-only.

## Auth
| Step | Detail |
|---|---|
| Login | `POST /login` (SvelteKit form action), `application/x-www-form-urlencoded`: `email`, `password`. **Needs an `Origin` header** equal to the portal origin (SvelteKit CSRF check) or it is rejected. |
| Success | `200 {"type":"redirect","status":303,"location":"/meters"}` + `Set-Cookie: __Secure-better-auth.session_token=...; Max-Age=3600; HttpOnly; Secure`. (better-auth cookie.) |
| Failure | `401 {"type":"failure",...,"Invalid email or password."}` |
| Session | Cookie only, **expires after 1 hour**. Expired/bad cookie -> `401 {"error":"unauthorized"}` on `/portal/*`. |
| Sign out | `POST /api/auth/sign-out` (not used) |

## Data endpoints (all `GET`, cookie required, JSON)
| Endpoint | Returns |
|---|---|
| `/portal/meters/search?q=&page=N` | 20 per page, `{data,total,page,pageSize}`. Substring match on meter number/serial. Only `meterId, serialNo, make, phaseType, installStatus, dtCode`. `limit`/`offset` are ignored; only `page` works. 403 meters -> 21 pages. |
| `/portal/meters/{id}/geo` | `{latitude, longitude}` as **strings**. |
| `/portal/meters/{id}/energy` | Time series (strings!) `{timestamp:"dd/mm/yyyy HH:MM", kwh, kvah, voltR}`. |
| `/portal/dts?page=N` | Distribution transformers: `code,name,feederCode,capacityKva`, 20/page, 40 total. |
| `/portal/keys` | `{"data":{"signingSecret":"..."}}` - hands the logged-in browser an HMAC secret (used by the export button). |
| `/portal/export?page=N` | **Bulk path.** All meters with `installType`, `build`, full 7-level `hierarchy` and `geo` as numbers. Requires signing (below). |

### Signed export
Headers `x-timestamp` (unix seconds) and `x-signature` = hex `HMAC-SHA256(signingSecret, "GET\n/portal/export\n<query string>\n<timestamp>")`,
e.g. query string `page=1`. Missing/bad signature -> `401 {"error":"signature_invalid"}`.
One call replaces ~21 search pages + 403 geo calls, and is the only place `hierarchy`, `installType` and `build` appear.

## Quirks / surprises
1. **Export ignores `page`.** Page 1 already returns all 403 meters; every other page returns the same 403 again. (`total` is the real total.)
2. **Rate limit: 429 after ~120 requests in a minute** (`{"error":"rate_limited"}`, no `Retry-After`). A full readings crawl is ~400 calls, so the client throttles itself.
3. **Readings granularity differs per meter**: the first 40 meters (J100000-J100039) return 337 half-hourly points (23-30 Jun 2026); the other 363 return 8 daily points at 00:00. Values are cumulative registers (monotonic), not per-interval consumption.
4. All numbers in `/energy` and `/geo` are strings; timestamps are `dd/mm/yyyy` with no timezone (assumed IST, see README).
5. **Hierarchy is inconsistent.** Each meter carries its own copy of the path. The same node (e.g. `D-01`) appears under 3 different circles, `F-001` under 2 substations, etc.; 14 export rows have an empty `code` (name still says `Circle 6`); a few legacy-build DT names differ from `/portal/dts` ("Old Malviya Nagar Xfmr"); 5 meters' feeder disagrees with `/portal/dts.feederCode`. We rebuild the tree by majority vote and report every disagreement at `/v1/data-quality`.
6. `build` is `legacy` (238) or `v2` (165); payload shape is identical for both.
7. Sustained crawling trips the 429 above mid-run (my first un-throttled sync lost 7 meters' readings this way); the client now self-throttles, so a full sync completes cleanly.
8. Unknown meter -> `404 {"error":"not_found"}`.
