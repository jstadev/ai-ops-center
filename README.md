# AI Ops Center

Automated property listing monitor for Spitogatos.gr (Crete), with Redis queue and GitHub Actions CI scraping.

---

## Architecture

```
GitHub Actions (every 30 min)
  └─ playwright headless + stealth
  └─ Tailscale tunnel
        └─ VPS Redis (91.98.119.81)
              ├─ spitogatos:seen_listings  (Set)
              └─ spitogatos:new_listings   (List → Discord bot reads)
```

Local Mac version (`agents/monitor/spitogatos_mac.py`) runs with `headless=False` on your residential IP — use this when Actions gets blocked by Kasada.

---

## Setup: GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name        | Value                              | Where to get it                                          |
|--------------------|------------------------------------|----------------------------------------------------------|
| `TAILSCALE_AUTH_KEY` | `tskey-auth-...`               | https://login.tailscale.com/admin/settings/keys → **Generate auth key** → check **Reusable** + **Ephemeral** |
| `REDIS_PASSWORD`   | your Redis password                | Same password set in your Docker Redis container         |
| `VPS_TAILSCALE_IP` | `100.113.88.103`                   | Tailscale admin console → Machines → your VPS            |

### Getting a Tailscale auth key

1. Go to https://login.tailscale.com/admin/settings/keys
2. Click **Generate auth key**
3. Set:
   - **Reusable**: YES (so every Actions run can use it)
   - **Ephemeral**: YES (so runner nodes auto-expire from your network)
   - **Expiry**: 90 days is fine, calendar-remind yourself to rotate
4. Copy the key (shown only once) → paste into GitHub Secret `TAILSCALE_AUTH_KEY`

---

## Files

```
.github/workflows/spitogatos.yml       # GitHub Actions workflow (every 30 min)
agents/monitor/spitogatos_scraper.py   # CI scraper (headless, reads secrets from env)
agents/monitor/spitogatos_mac.py       # Mac scraper (headless=False, residential IP fallback)
```

---

## Push to GitHub

```bash
cd ~/ai-ops-center

git init                          # if not already a git repo
git remote add origin git@github.com:YOUR_USERNAME/ai-ops-center.git

git add .
git commit -m "Add Spitogatos GitHub Actions scraper"
git push -u origin main
```

The workflow triggers automatically on schedule. To test immediately:

**Actions tab → Spitogatos Scraper → Run workflow**

---

## Redis keys

| Key                          | Type | Purpose                                      |
|------------------------------|------|----------------------------------------------|
| `spitogatos:seen_listings`   | Set  | IDs of all listings ever scraped (dedup)     |
| `spitogatos:new_listings`    | List | Queue of new listing JSON, LPUSH / BRPOP     |

To inspect on VPS:
```bash
redis-cli -a YOUR_PASSWORD
SCARD spitogatos:seen_listings          # how many seen
LLEN spitogatos:new_listings            # how many in queue
LRANGE spitogatos:new_listings 0 2      # peek at top 3
```

---

## Bot protection notes

Spitogatos uses **Kasada** bot protection backed by CloudFront.

| Runner         | IP type      | Likely outcome        |
|----------------|--------------|-----------------------|
| GitHub Actions | Datacenter   | May get 403 / JS wall |
| Mac (local)    | Residential  | Usually passes        |

If Actions returns 0 cards consistently:
1. Check the HTML snippet in the workflow log — if it contains `kasada` or `challenge`, it's blocked
2. Fall back to the Mac version (`python agents/monitor/spitogatos_mac.py`) running as a local background process
3. Or add a residential proxy via `PLAYWRIGHT_PROXY` env var in the workflow
