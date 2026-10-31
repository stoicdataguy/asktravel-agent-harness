# Deployment plan: AskTravel beta on AWS

Status: proposed (2026-10-30). Covers issues #27–#32 and #14, plus #90 (spike), #91 (deploy
pipeline) and #92 (beta launch); see §9. Key terms and the reasoning behind each
decision are explained visually in [deployment-explainer.html](../deployment-explainer.html).

**Goal:** a private beta for 50–100 invited users at `https://asktravel.<your-domain>`, deployed
from GitHub on every merged PR, for about $10–15 a month in hosting (LLM usage is separate,
§7).

**The short version**

- **One small server running the stack you already run locally:** an AWS Lightsail instance in
  us-east-1 with Docker Compose. Caddy serves HTTPS in front of Open WebUI. The agent API and
  Postgres are not reachable from the internet.
- **Managed free tiers for the heavy or fiddly parts:** LangSmith for tracing, GitHub
  Actions and GHCR for CI/CD and images, Tailscale for SSH.
- **CI on every PR, deploy on every merge to `main`:** a manual approval step, then an automatic
  health check and rollback.
- **Nightly database backups to S3, and a restore drill** before inviting anyone.
- **Hosting cost:** $12/month for the 2 GB server, or $24 if the memory check in D0 says 4 GB is
  needed. Plus about $1 for the domain and cents for S3.
- **Being honest about $10/month:** it isn't reachable on AWS with enough memory. §3 explains why,
  and covers the one provider where it is.

---

## 1. What has to run in production

| Piece | Today (local) | Memory (measured 2026-10-30) | In production |
|---|---|---|---|
| Open WebUI | compose, `:main` tag | ~700 MB idle | Same container, pinned version |
| Agent API (FastAPI) | `uv run travel-agent` (reload on) | ~300 MB | Container built by CI, no reload |
| Chromium (hotel search) | started per `search_hotels` call | Up to ~1.5 GB for 2 parallel searches (macOS count, which overstates shared memory; Linux is lower) | Same, capped at 1 browser at a time |
| Postgres 16 | compose, port 5433 open | ~85 MB | Same container, no published port, tuned small |
| Tracing | LangSmith (cloud) since #87; self-hosted Langfuse before, ~4.2 GB | none | **LangSmith** (free Developer plan), nothing to host |
| Guidebooks | `data/guidebooks/*.md`, 16 MB | (in the API) | Copied to the server by hand; never in an image (copyrighted) |
| Background loops | chat-deletion sync, consolidation | (in the API) | Same: needs an always-on process (rules out scale-to-zero hosting) |

Tracing used to be the one thing that couldn't come along: self-hosted Langfuse needed more
memory than the rest of the app combined. Since #87 traces go to LangSmith, so nothing is left
to move.

Peak memory is about Open WebUI 0.7 + API 0.3 + Postgres 0.1 + OS 0.3 GB,
plus Chromium during hotel searches:

- one browser at a time: ~0.4–0.6 GB;
- two in parallel: ~1 GB or more.

So **2 GB fits only with hotel searches serialised and swap as a safety net. 4 GB fits
comfortably.** D0 measures this on the real Linux server before you commit to a size.

Other properties of the app that shape the design:

- **Long requests.** An agent turn streams for 30–90 s. Every proxy must allow long-lived
  streaming responses; Caddy does by default.
- **Stateful single process.** The background loops and the in-process prompt cache assume one
  API instance. That's fine for a beta, and it is why §3 doesn't start with autoscaling.
- **Outbound calls.** The app calls OpenRouter, LangSmith and Google (flights and hotels). It
  receives no inbound calls except from users, through Open WebUI.

## 2. Target architecture

```
                        your laptop ──git push──▶ GitHub (PR → CI → merge → deploy)
                                                       │ image → GHCR
                                                       │ ssh over Tailscale (deploy)
 users ──HTTPS──▶ DNS: asktravel.<domain> ──▶ Lightsail static IP
                                              │ firewall: 80, 443 only
              ┌───────────────────────────────▼──────────────────────────────┐
              │ Lightsail instance (Ubuntu 24.04, 2 GB, us-east-1)           │
              │                                                              │
              │  caddy :80/:443 ── HTTPS (Let's Encrypt), reverse proxy      │
              │    │                                                         │
              │    ▼  internal Docker network only                           │
              │  open-webui :8080 ──Bearer token──▶ api :8000 ──▶ postgres   │
              │                                       │   (open_webui schema │
              │                                       │    + agent tables)   │
              │  backup job (nightly) ── pg_dump ─────┼──────────▶ S3 bucket │
              └───────────────────────────────────────┼──────────────────────┘
                                                      ▼ outbound HTTPS
                          OpenRouter (LLM) · LangSmith (traces) · Google (flights, hotels)
```

What is public and what isn't:

| Endpoint | Reachable from | Why |
|---|---|---|
| `https://asktravel.<domain>` (Open WebUI) | Internet | The product |
| `https://asktravel.<domain>/agent-health` | Internet | Caddy routes it to the API's `/health` and nothing else, so an uptime monitor can check the API |
| Agent API `:8000` | Open WebUI only (Docker network) + bearer token | It trusts the user-email header (#27). From the internet, anyone could read or delete anyone's memory |
| Postgres `:5432` | API and Open WebUI only | No published port in prod |
| SSH `:22` | Your tailnet only (laptop + CI runner) | Closed in the Lightsail firewall; no SSH brute-force surface at all |

## 3. Decisions (and the options not taken)

Each decision is written as: what we chose, what we gave up, and when to revisit it. The
explainer page shows the same decisions with diagrams.

### D-1. One server with Docker Compose, not managed containers

- **Chosen:** a single VM running `docker compose`, the same as your laptop.
- **Not now:** ECS Fargate + RDS + ALB, as in the earlier [PLAN.md](../PLAN.md#deployment-on-aws)
  design. The load balancer alone (~$16–20) exceeds the budget. With two Fargate tasks
  (~$70), RDS (~$15) and NAT or VPC endpoints, it comes to ~$110–150 a month.
- **Given up:** high availability (one server = one point of failure), zero-downtime deploys,
  automatic scaling. A restart or deploy causes 10–30 s of downtime.
- **Why it's right for a beta:** 100 invited users don't need 99.9% uptime. The skills you learn
  carry over to managed services when you move there: containers, images, DNS, TLS, secrets,
  CI/CD, backups, monitoring.
- **Revisit when:** paying users, an uptime promise, or more traffic than one server handles.

### D-2. AWS Lightsail (2 GB, $12/month) over EC2 or another provider

| Option | Monthly | RAM | What you learn | Catch |
|---|---|---|---|---|
| **Lightsail 2 GB** (chosen) | **$12** | 2 GB, 2 vCPU, 60 GB SSD, 3 TB transfer | Core cloud: instance, static IP, firewall, DNS, snapshots, resizing; plus S3 and IAM for backups | Tight on memory; burstable CPU; few AWS-specific concepts |
| Lightsail 4 GB | $24 | 4 GB, 80 GB SSD | Same | 2.4× the budget; the fallback if D0 says 2 GB is too tight |
| EC2 t4g.small (ARM) | ~$17.50 (instance $12.30 + public IPv4 $3.65 + 20 GB disk $1.60) | 2 GB | Real AWS: VPC, security groups, IAM roles (backups without keys), SSM Session Manager (no SSH), CloudWatch | More moving parts on day one; images must be built for arm64 |
| Hetzner CX23 / CAX11 | ~€6–7 incl. IPv4 | 4 GB | The same VM skills, not AWS | The cheapest RAM, but that price is in EU data centres only. Google shows cookie-consent walls to EU IPs, which breaks the scrapers. Plans were intermittently unavailable to order in Sep/Oct 2026 |
| Oracle Cloud Always Free (ARM) | $0 | up to 24 GB | VM skills | Capacity is often unavailable, and idle instances can be reclaimed: not something to run a beta on |

**Why Lightsail:**

- It's AWS, which you're leaning toward and which is worth knowing.
- It's the simplest AWS product, with a fixed monthly price. A beginner can't run up a surprise
  bill with a NAT gateway or data transfer.
- A US region keeps the Google scrapers working as they do from your laptop.
- If 2 GB proves too small, the move is cheap: snapshot, create a 4 GB instance from it, move the
  static IP. DNS doesn't change.

**Being honest about the budget:** Lightsail's AWS-specific lessons are thin. It's essentially a
VPS (a rented virtual server) with an AWS bill. If $10/month matters more than AWS, choose
Hetzner, from a US location (pricier than the EU plans) or with the consent wall handled.
The rest of this plan is unchanged on Hetzner: replace §6's Lightsail steps with Hetzner's
equivalents.

**Graduation path:** once the beta has run for a month, moving to EC2 (D-2 row 3) is a good
next lesson. Do it as infrastructure as code (Terraform or CDK), which the single Lightsail
server doesn't need yet.

### D-3. LangSmith (free) for tracing

Decided and built in #87, which replaced self-hosted Langfuse.

- **Why:** self-hosting Langfuse means six containers and ~4 GB of memory (ClickHouse alone
  2.5 GB). LangSmith is cloud only and native to LangChain: one trace per agent run, with the
  graph, middleware and tool calls nested in it.
- **Free Developer plan:** 5k traces a month, 14 days of history, 1 seat.
- **Usage estimate:**
  - One trace per agent turn (measured in #87). Consolidation calls and evals are not traced
    (evals only with `LANGSMITH_TRACE_EVALS=true`).
  - So 5k traces ≈ **5,000 turns a month**, twice what 100 users × 25 turns would use.
  - Dev and prod share the account's quota. Use separate projects (`LANGSMITH_PROJECT=asktravel-prod`
    on the server) so the beta's traces stay apart from local testing.
  - If you approach it: sample traces (the LangSmith client's `tracing_sampling_rate`, a small
    `observability.py` setting), or pay for the extra traces (see the LangSmith pricing page).
- **Key:** the server gets a write-only **tracing key**. It can send traces but not read them,
  so a leaked server key exposes nothing. The startup check accepts this kind of key (#87).
- **Privacy:** traces contain users' messages. Say so in the beta invite. `DELETE /api/memory`
  doesn't reach LangSmith, but traces expire after 14 days.
- **Don't** also turn on OpenRouter Broadcast: it sends each model call as its own trace and
  would use the quota about five times faster.

### D-4. GitHub Actions for CI/CD, images in GHCR

- **Why:** the repo is public, so Actions minutes and public GHCR images are free.
- **The image contains:** code and dependencies only. No secrets, no guidebooks.
- **Pushes and pulls:**
  - The workflow pushes with its built-in `GITHUB_TOKEN`; no stored credentials.
  - The server pulls anonymously, because the image is public.
- **Not chosen:** AWS ECR. Pulling from it on Lightsail needs a long-lived AWS access key on the
  server, and it adds nothing at this size.

### D-5. Push-based deploys over Tailscale, with an approval gate

**How a deploy runs:**

1. After CI passes on `main`, the deploy job waits in GitHub's `production` environment until
   you approve it. Required reviewers are free on public repos.
2. It joins your tailnet (Tailscale's private network) as a short-lived, tagged device.
3. It SSHes to the server and runs `deploy/deploy.sh <sha>` there.

**Why push-based:**

- You see each deploy in the GitHub UI.
- One click approves it.
- Rolling back is re-running an old commit's deploy.

**Why Tailscale:** port 22 stays closed to the internet. GitHub's runners use thousands of
changing IPs, so the alternative is SSH open to the world.

**Not chosen:**

- Pull-based auto-updaters (a container on the server that watches the registry): no approval
  step and no visibility.
- AWS CodeDeploy: not available for Lightsail without extra setup.

### D-6. Caddy for HTTPS

- **Why:** it gets and renews Let's Encrypt certificates automatically from a 3-line config,
  streams responses without buffering, and uses ~30 MB.
- **Not chosen:**
  - Nginx + certbot: more configuration to learn at once.
  - A Lightsail load balancer with a certificate: $18/month.

### D-7. No staging server

- **Why:** a second server doubles the cost.
- **Instead:** "staging" is the production compose file running on your laptop (D3's acceptance
  test), plus the approval gate.
- **Revisit with:** more users or contributors. Add a second, smaller Lightsail instance and a
  `staging` environment in the same workflow.

### D-8. Secrets in a `.env` file on the server

- **Where they live:** `/opt/asktravel/.env`, owned by the `deploy` user, `chmod 600`, never in
  git or images.
- **GitHub holds only what CI needs:** the Tailscale OAuth client and the SSH deploy key.
- **Not chosen:** AWS Secrets Manager or SSM Parameter Store. Lightsail can't attach an IAM
  role, so reading them needs a stored AWS key anyway, and the server would still keep a copy.
- **Revisit with:** EC2 (D-2's graduation path), where the instance role reads Parameter Store
  without stored keys.

## 4. Blockers to resolve before inviting anyone

These aren't engineering tasks, but each one can stop the beta.

1. **Guidebook copyright.** `data/guidebooks` holds Lonely Planet and other commercial guides.
   Using them on your own laptop is one thing. Serving excerpts and page citations to 100 other
   people is distribution.
   - Options: get permission, run the beta with guidebook tools off (a setting that hides the
     tools), or replace the guides with sources you're allowed to use, such as Wikivoyage
     (CC BY-SA).
   - Decide in D0. (Not legal advice; if in doubt, ask someone qualified.)
2. **Open WebUI's licence and your logo.** `docker-compose.yml` replaces Open WebUI's logo. Its
   licence allows custom branding only for deployments of up to 50 users in a rolling 30 days;
   more needs an enterprise licence ([docs.openwebui.com/license](https://docs.openwebui.com/license)).
   - Either cap the beta at 50 users with the AskTravel logo, or drop the logo overrides in prod
     (keeping `WEBUI_NAME` and the "(Open WebUI)" attribution) before going past 50.
3. **Scraping Google.** `search_flights` and `search_hotels` read Google's pages, and Google often
   challenges traffic from cloud IP ranges ("unusual traffic", captchas).
   - D0 tests this from the real server. If it's blocked, the tools return their error strings
     and the agent still works without live prices, a weaker product.
   - Scraping also sits against Google's terms of service; keep volumes low and be aware of the
     risk.
4. **Model rate limit (#51).** The provider allows ~20 model calls a minute, and a turn averages
   ~3.7 calls (55 calls over 15 turns in the longest chat). So the whole beta can run about
   **5 turns a minute**, roughly 10 people actively chatting at once.
   - Beyond that, users wait behind `⏳` retries.
   - Raise the limit, add a fallback model (#51) or invite in waves (§8 D7). Check this before
     the 50-user wave.

## 5. Changes to the code (PRs before the first deploy)

| PR | Issue | What |
|---|---|---|
| API auth + invite-only signup | #27 | Require `Authorization: Bearer $AGENT_API_KEY` on everything except `/health`. Open WebUI sends it via `OPENAI_API_KEY`. Prod compose: `DEFAULT_USER_ROLE=pending` (you approve each sign-up in the admin panel), `WEBUI_SECRET_KEY` set |
| CI | #29 | `.github/workflows/ci.yml`: on pull requests and pushes to `main`, `uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest` (offline; no Postgres or browser needed). Then make it a required check in the "Protect main" ruleset |
| Dockerfile + prod compose | #28 | See §5.1–5.3. Run the e2e suites against it locally before merging |
| Memory diet | new (part of #28) | `HOTEL_BROWSERS=1`: an `asyncio.Semaphore` around the browser in `search_hotels`, so parallel calls queue instead of starting two Chromiums. Compose memory reservations; Postgres `shared_buffers=128MB`; Open WebUI with RAG/embedding features off |
| Per-user cap + health route | #32 | Daily turn limit per email (e.g. 40), answered with a clear chat message; `/agent-health` route in Caddy |
| Guidebook switch | new, if §4.1 needs it | `GUIDEBOOKS_ENABLED=false` hides the three guidebook tools and the guidebook skills' references |

### 5.1 `Dockerfile` (agent API)

- **Build:** a multi-stage build with uv (`uv sync --frozen --no-dev` into `/app/.venv`) on
  `python:3.12-slim-bookworm`, then `playwright install --with-deps chromium`.
- **Runtime:**
  - Runs as a non-root `app` user.
  - Starts with `uvicorn travel_agent.api.app:app --host 0.0.0.0 --port 8000`, with no
    `--reload`. Today's `travel_agent:main` reloads, which prod must not do.
  - `HEALTHCHECK` curls `/health`.
- **Kept out of the image** (via `.dockerignore`): `data/`, `reference/`, `.env`, `notebooks/`,
  `docs/`, `.venv`, `tests/`.
- Built for `linux/amd64` (Lightsail is x86).

### 5.2 `docker-compose.prod.yml`

Differences from the dev file:

- **Tracing:** LangSmith settings in `.env`; nothing to run.
- **Pinned versions:**
  - Open WebUI is pinned to a release (e.g. `ghcr.io/open-webui/open-webui:v0.12.4`), not
    `:main`. Upgrades are their own PR, followed by `e2e_chat_deletion.py`.
  - `api` uses `ghcr.io/stoicdataguy/asktravel-api:${IMAGE_TAG}`.
- **Published ports:** only Caddy's 80/443; nothing else is published.
- **Services:**
  - **`api`** mounts `./guidebooks:/app/data/guidebooks:ro`, has `stop_grace_period: 60s` (lets
    a running turn finish), and `restart: unless-stopped`.
  - **`caddy`** (`caddy:2`) has volumes for its certificates and its config file
    (`deploy/Caddyfile`).
  - **`postgres`**: a strong password from `.env` and `restart: unless-stopped`.
- **Logs:** `json-file` with `max-size: 10m`, `max-file: 3` on every service, so they can't fill
  the disk.
- **Open WebUI** reaches the API at `http://api:8000/v1` instead of `host.docker.internal`.

### 5.3 `deploy/` folder (new)

- `Caddyfile`:
  ```
  asktravel.<domain> {
      handle /agent-health { rewrite * /health
                             reverse_proxy api:8000 }
      reverse_proxy open-webui:8080
  }
  ```
- `bootstrap.sh`: turns a fresh Ubuntu server into an AskTravel host. It's idempotent, so
  re-running it is safe. Its steps:
  - Install Docker from Docker's own repository.
  - Turn on automatic security updates (`unattended-upgrades`).
  - Add 2 GB of swap.
  - Create a `deploy` user.
  - Install Tailscale.
  - Create `/opt/asktravel`.
  - Install the nightly backup timer.

  Committing it is how this one server becomes reproducible: "infrastructure as code, lite".
- `deploy.sh <sha>` runs on the server for every deploy:
  1. `git fetch && git checkout <sha>`, to get the compose file, Caddyfile and branding.
  2. A quick `pg_dump`, a safety net just before the change.
  3. `IMAGE_TAG=<sha> docker compose pull && docker compose up -d --wait`.
  4. Health checks: `curl -fsS localhost/agent-health`, then Open WebUI's `/health`.
  5. If either fails, redeploy the previous good tag (in `.last-good`) and exit non-zero, so
     the workflow fails red.
  6. Record `<sha>` in `.last-good`; `docker image prune -f`.
- `backup.sh`: `pg_dump -Fc` → `aws s3 cp` to `s3://asktravel-backups-<id>/daily/<date>.dump`,
  then pings a Healthchecks.io URL (it alerts if a night is missed).
- `runbook.md`: how to deploy, roll back, restore, read logs, rotate secrets, resize the
  server, and handle a full disk.

## 6. AWS and server setup (done by hand once, written down in the runbook)

1. **AWS account hygiene** (30 min, before anything else):
   - **Root user:** MFA on, never used again.
   - **Your everyday login:** an admin user via IAM Identity Center, with MFA.
   - **Budget alerts:** an AWS Budget of $15/month with email alerts at 50%, 80% and 100%
     (actual) and at 100% (forecast).
   - Lightsail's prices are fixed, but S3 and mistakes aren't.
2. **Lightsail:**
   - **Instance:** Ubuntu 24.04 LTS in `us-east-1`, the $12 plan (or $24 after D0), with your
     SSH key.
   - **Networking:** attach a static IP; it's free while attached.
   - **Firewall:** allow 80 and 443 (IPv4 and IPv6). Delete the default SSH rule *after*
     Tailscale works.
3. **Domain and DNS:**
   - Buy a domain (Route 53 or Cloudflare Registrar, ~$10–15/year).
   - Create an `A` record `asktravel` → the static IP (also `AAAA` if you enable IPv6), in a
     Lightsail DNS zone (free) or wherever the domain lives.
4. **S3 for backups:**
   - **Bucket:** `asktravel-backups-<random>`, Block Public Access on (the default),
     versioning on.
   - **Lifecycle rule:** delete after 35 days.
   - **IAM user:** `asktravel-backup` with one policy, `s3:PutObject` on
     `arn:aws:s3:::asktravel-backups-…/daily/*` only. A stolen key can add backups but not read
     or delete them: least privilege in practice.
5. **Tailscale:**
   - Install on your laptop and (via `bootstrap.sh`) on the server.
   - For CI, create an OAuth client with tag `tag:ci`, and an ACL that lets `tag:ci` reach only
     the server's port 22.
6. **Server:**
   - Run `bootstrap.sh`, then clone the repo into `/opt/asktravel`.
   - Copy in `.env` (the template is `.env.example` plus the prod-only keys below).
   - `scp` the guidebooks to `/opt/asktravel/guidebooks`.
7. **External accounts:**
   - **OpenRouter:** a *separate* production key with a credit limit, so a bug or abuse can't
     drain your account.
   - **LangSmith:** a project `asktravel-prod` and a tracing key for the server.
   - **Monitoring:** UptimeRobot (free) on `https://asktravel.<domain>/agent-health`, alerting
     your email; Healthchecks.io (free) for the backup job.

Production-only settings (also added to `.env.example`, commented):

```
AGENT_API_KEY=...            # shared by Open WebUI (OPENAI_API_KEY) and the API (#27)
POSTGRES_PASSWORD=...        # replaces travel/travel
WEBUI_SECRET_KEY=...         # keeps Open WebUI sessions valid across restarts
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...        # a write-only tracing key
LANGSMITH_PROJECT=asktravel-prod
HOTEL_BROWSERS=1
DAILY_TURN_LIMIT=40
DOMAIN=asktravel.<domain>
```

## 7. Cost

| Item | Monthly (USD) |
|---|---|
| Lightsail 2 GB instance (static IP included while attached) | $12.00 (4 GB: $24.00) |
| Domain (~$12/year) | ~$1.00 |
| S3 backups (~35 dumps, well under 5 GB) | < $0.25 |
| DNS (Lightsail zone) | $0 |
| GitHub Actions + GHCR (public repo) | $0 |
| Tailscale (personal plan), UptimeRobot, Healthchecks.io | $0 |
| LangSmith Developer | $0 (pay per extra trace over 5k a month) |
| **Hosting total** | **~$13** (4 GB: ~$25) |
| OpenRouter (LLM) | **Usage-based; likely more than hosting.** Read your real cost per chat from OpenRouter's activity page or LangSmith, multiply by expected chats, and set the prod key's credit limit to that plus a margin |

Prices are from the Lightsail and LangSmith pricing pages, checked 2026-10-30. New AWS accounts
may get free-tier credits or free Lightsail months; check your eligibility, but don't plan
around them.

## 8. Phases

Each phase ends with checks you can run. They follow the repo's workflow: issue → branch →
PR → approval → merge.

### D0. Decide and spike (1–2 evenings, ~$0.10) — #90

1. **Decide the blockers in §4.** Guidebooks: on, off or replaced. Beta size or branding.
2. **Set up the AWS account (§6.1).**
3. **Measure the real server.**
   - Create a throwaway $12 Lightsail instance in us-east-1. It's billed by the hour; delete it
     the same day.
   - Install Docker and Playwright, and run `fetch_hotel_cards` twice in parallel and once
     alone, plus a `search_flights` call. Watch `free -m` and `docker stats`.
4. **Check Google from an AWS IP.** Do the flight and hotel results come back, or is there a
   captcha or consent page?

**Exit checks**

- [ ] Scrapers work from AWS (or the fallback is decided).
- [ ] Peak memory with one browser fits under ~1.7 GB, so 2 GB will do; otherwise choose 4 GB.
- [ ] Budget alerts are live.

### D1. API auth and invite-only signup — #27

- [ ] Requests without the token get 401.
- [ ] `e2e_branching.py` passes.
- [ ] A new sign-up lands in "pending" until approved.

### D2. CI — #29

- [ ] A PR with a failing test shows a red check and can't be merged.
- [ ] A clean PR goes green in under ~3 min.

### D3. Containerise and run prod compose locally — #28 + memory diet

- [ ] `docker compose -f docker-compose.prod.yml up` on your laptop, with Caddy on
  `localhost`, serves the full app.
- [ ] `e2e_progress.py`, `e2e_branching.py`, `e2e_memory.py` and `e2e_hotels.py` pass against it.
- [ ] Two parallel hotel searches run one after the other and both succeed.

### D4. First deploy, by hand — #30

Do every step yourself once before automating it: the best way to learn what the pipeline does.

1. Set up AWS and the server (§6.2–6.7).
2. On the server: `IMAGE_TAG=<sha> docker compose -f docker-compose.prod.yml up -d`, with an
   image you pushed from your laptop.
3. Create your admin account first, while signup is still in your hands.

**Exit checks**

- [ ] `https://asktravel.<domain>` loads with a valid certificate.
- [ ] Port 8000, 5432 and 22 are closed from outside (`nc -vz <ip> 8000` fails).
- [ ] `e2e_flights.py` and `e2e_hotels.py` pass when run on the server. They need small
  `--base-url` / `--api` options added.

### D5. Continuous deployment — #91

`.github/workflows/deploy.yml`:

```yaml
on:
  push: { branches: [main] }        # i.e. a PR was merged
  workflow_dispatch:                 # manual: redeploy any commit (rollback)
    inputs: { sha: { required: false } }
concurrency: { group: production, cancel-in-progress: false }
jobs:
  test:    # same steps as ci.yml (reuse it with `uses: ./.github/workflows/ci.yml`)
  build:   # needs: test → docker buildx → ghcr.io/stoicdataguy/asktravel-api:<sha> and :main
  deploy:  # needs: build; environment: production (you approve)
           # tailscale/github-action (tag:ci) → ssh deploy@asktravel "/opt/asktravel/deploy/deploy.sh <sha>"
           # then curl -fsS https://asktravel.<domain>/agent-health from the runner
```

**Exit checks**

- [ ] Merging a PR that changes a prompt shows up in prod after one approval click, with no
  terminal.
- [ ] A deliberately broken image (failing health check) is rolled back automatically, and the
  run goes red.
- [ ] `workflow_dispatch` with an older SHA rolls back.

### D6. Backups, restore drill, monitoring, limits — #31, #32

- [ ] The nightly backup appears in S3, and a missed night alerts Healthchecks.io.
- [ ] **Restore drill:** a dump restored into a scratch Postgres on your laptop. The notebook
  shows the same users, facts and threads. Write down how long it took: your recovery time.
- [ ] UptimeRobot emails you within 5 min of `docker compose stop api`.
- [ ] The 41st turn of the day gets the "daily limit reached" message.

### D7. Beta launch — #92

1. **Waves:** 5 friends → 25 → 50 (→ 100 once §4.2 and §4.4 are resolved).
2. **Invite note:** what AskTravel is, that conversations are stored and traced, how to delete
   your data, and where to send feedback (👍/👎 in the chat; the Feedback phase F1–F4 makes those
   useful).
3. **Weekly 15-minute review:**
   - Lightsail memory and burst-CPU graphs.
   - OpenRouter spend.
   - LangSmith trace count (against 5k a month) and errored runs.
   - The `⏳` retry counts in the API log (#51).
   - Disk space.

**Exit checks**

- [ ] A week at 25 users with no unplanned downtime longer than 10 min.
- [ ] Spend within the forecast.

## 9. Issues

| Issue | Phase | Points |
|---|---|---|
| #90 Prod D0: spike, server size and Google scraping from AWS | D0 | 1 |
| #91 Prod D5: continuous deployment (deploy.yml, deploy.sh, Tailscale, rollback) | D5 | 3 |
| #92 Prod D7: beta launch, invite waves, privacy note, weekly review | D7 | 1 |
| Not filed yet: guidebook switch (only if D0 decides "off" in §4.1) | D3 | 1 |

The existing #27–#32 keep their scope, and #30's "4 GB VM" becomes "2 or 4 GB, decided in D0".
#14's ECS design stays in PLAN.md as the graduation path.

## 10. What you'll have learned by the end

- **Cloud fundamentals:** regions, instances, static IPs, firewalls, DNS records, TLS
  certificates, object storage, IAM least privilege, budgets.
- **Containers in production:** images vs containers, registries, tags vs digests, health
  checks, restart policies, resource limits, log rotation.
- **CI/CD:** workflows, runners, jobs and dependencies, environments and approvals,
  concurrency, artifacts (images) promoted by commit SHA, automatic rollback.
- **Operations:** backups that are tested, uptime and job monitoring, runbooks, rate and spend
  limits, upgrade discipline (pinned versions).
- **What to change at the next level:** EC2 + IAM roles, infrastructure as code, managed
  Postgres, a load balancer with two instances, staging. Each one now replaces something you
  understand.
