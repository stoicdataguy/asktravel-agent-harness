# Deployment plan: AskTravel beta on AWS

Status: proposed (2026-10-30). Covers issues #27–#32 and #14, plus #90 (spike), #91 (deploy
pipeline) and #92 (beta launch); see §9. Key terms and the reasoning behind each
decision are explained visually in [deployment-explainer.html](../deployment-explainer.html).

**Goal:** a private beta for 50–100 invited users at `https://asktravel.<your-domain>`, deployed
from GitHub on every merged PR, for about $20 a month in hosting (LLM usage is separate, §7).
A second goal: learn the core AWS services by using them for real.

*Revised 2026-10-30 (#98): EC2 with AWS-native services replaced Lightsail, Tailscale and GHCR,
so that the beta teaches real AWS. The Lightsail version costs ~$13 and stays in D-2 as the
simpler option.*

**The short version**

- **One small server running the stack you already run locally:** an EC2 `t4g.small` (2 GB,
  arm64) in the default VPC of us-east-1, running Docker Compose. Caddy serves HTTPS in front of
  Open WebUI. The security group opens 80 and 443 only. The agent API and Postgres are not
  reachable from the internet.
- **No SSH and no stored AWS keys anywhere:**
  - You get a shell through **SSM Session Manager**.
  - GitHub Actions logs in to AWS with **OIDC** and deploys with **SSM Run Command**.
  - The server uses an **IAM role** for everything it needs.
- **AWS services for the rest:** ECR for images, Route 53 for DNS, SSM Parameter Store for
  secrets, S3 for backups, EBS snapshots, CloudWatch for alarms and logs, AWS Budgets.
- **CI on every PR, deploy on every merge to `main`:** a manual approval step, then an automatic
  health check and rollback.
- **Nightly database backups to S3, daily disk snapshots, and a restore drill** before inviting
  anyone.
- **Hosting cost:** about **$21/month** for the 2 GB server, or about $33 if the memory check in
  D0 says 4 GB is needed. New AWS accounts get up to $200 in credits for 6 months (§7).

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
  your laptop ──git push──▶ GitHub (PR → CI → merge → approve → deploy)
                               │ OIDC → IAM role "asktravel-github-deploy" (no stored AWS keys)
                               │ docker push (arm64 image) ──────────────▶ ECR asktravel-api
                               │ ssm send-command "deploy.sh <sha>"
                               ▼
 users ──HTTPS──▶ Route 53: asktravel.<domain> ──▶ Elastic IP
  ┌─ AWS us-east-1 · default VPC · public subnet ──────────────────────────────────┐
  │                                   │ security group: 80, 443 in (no 22)          │
  │  ┌────────────────────────────────▼──────────────────────────────────────────┐ │
  │  │ EC2 t4g.small (Ubuntu 24.04 arm64, 2 GB) · 20 GB EBS gp3 · IMDSv2          │ │
  │  │ IAM role "asktravel-ec2": SSM, ECR pull, S3 put, Parameter Store read, logs│ │
  │  │                                                                            │ │
  │  │  caddy :80/:443 ── HTTPS (Let's Encrypt), reverse proxy                    │ │
  │  │    ▼ internal Docker network only                                          │ │
  │  │  open-webui :8080 ──Bearer token──▶ api :8000 ──▶ postgres                 │ │
  │  │  backup timer (nightly) ── pg_dump ──────────────────────▶ S3 bucket       │ │
  │  └───────┬──────────────────────────────────────────────────────────────────┘ │
  │          │ container logs, metrics, alarms ─▶ CloudWatch ─▶ SNS email         │
  │  DLM: daily EBS snapshots · SSM Parameter Store: secrets · AWS Budgets        │
  └───────────┬───────────────────────────────────────────────────────────────────┘
              ▼ outbound HTTPS
   OpenRouter (LLM) · LangSmith (traces) · Google (flights, hotels)

  you ──aws ssm start-session──▶ a shell on the server (no SSH, no key pair)
```

What is public and what isn't:

| Endpoint | Reachable from | Why |
|---|---|---|
| `https://asktravel.<domain>` (Open WebUI) | Internet | The product |
| `https://asktravel.<domain>/agent-health` | Internet | Caddy routes it to the API's `/health` and nothing else, so an uptime monitor can check the API |
| Agent API `:8000` | Open WebUI only (Docker network) + bearer token | It trusts the user-email header (#27). From the internet, anyone could read or delete anyone's memory |
| Postgres `:5432` | API and Open WebUI only | No published port in prod. For `psql` from your laptop, use an SSM port-forwarding session |
| SSH `:22` | Nobody | Not in the security group, and the instance has no key pair. Shells go through SSM Session Manager, which connects outbound from the server |

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
- **Why it's right for a beta:** 100 invited users don't need 99.9% uptime. Everything you learn
  carries over to managed services: VPCs, security groups, IAM roles, images, DNS, TLS,
  secrets, CI/CD, backups, monitoring.
- **Revisit when:** paying users, an uptime promise, or more traffic than one server handles.

### D-2. EC2 t4g.small (2 GB, arm64), not Lightsail

| Option | Monthly | RAM | What you learn | Catch |
|---|---|---|---|---|
| **EC2 t4g.small** (chosen) | **~$17.50** server part (instance $12.26 + public IPv4 $3.65 + 20 GB gp3 $1.60) | 2 GB, 2 vCPU (Graviton, arm64) | Real AWS: VPC, subnets, security groups, AMIs, EBS, Elastic IPs, IAM roles, IMDSv2, SSM, CloudWatch | More parts to set up; images must be built for arm64; burstable CPU credits |
| EC2 t4g.medium | ~$29.80 server part | 4 GB | Same | The fallback if D0 says 2 GB is too tight. Resizing is stop → change type → start |
| EC2 t3.small (x86) | ~$20.40 server part | 2 GB | Same | ~$3 more; the fallback if D0 finds an arm64 problem in a dependency |
| Lightsail 2 GB | $12, all included | 2 GB | Instance, static IP, firewall, DNS, snapshots; little AWS beyond that | The previous choice: cheapest AWS option, fixed price, but it hides the AWS concepts this plan now wants to teach |
| Hetzner CX23 / CAX11 | ~€6–7 incl. IPv4 | 4 GB | The same VM skills, not AWS | EU data centres at that price, where Google shows cookie-consent walls that break the scrapers |

**Why EC2:**

- Every AWS service you'll use later (ECS, RDS, load balancers, Lambda in a VPC) builds on the
  same VPC, security group and IAM role concepts. Here you learn them on one server.
- With an IAM role, the server needs **no AWS access keys**. That's also what makes ECR,
  Parameter Store and S3 backups clean. Lightsail can't attach a role.
- **arm64 (Graviton)** is ~20% cheaper than the x86 `t3.small` for the same memory. GitHub's free
  arm64 runners build the image natively (D-4).
- A US region keeps the Google scrapers working as they do from your laptop.

**The rules that keep the bill small** (the usual EC2 surprises):

- **No NAT gateway, ever** (~$33/month on its own). The server sits in the default VPC's public
  subnet, so it doesn't need one.
- **Every public IPv4 address costs $3.65/month** since 2024, including an Elastic IP. Keep exactly
  one, and release it if you terminate the instance (an unattached one still bills).
- **CPU credits:** `t4g` instances burst above 20% CPU on credits. Launch with credit
  specification **`standard`**, so a busy hour slows the server down instead of adding charges
  (`unlimited`, the default for t4g, bills the extra). A CloudWatch alarm warns when credits run
  low.
- **Delete what you test with.** D0's spike instance, its volume and its IP are billed per hour
  until you terminate and release them.

**Graduation path:** once the beta has run for a month, capture this setup as infrastructure as
code (Terraform or CDK). Then a second environment (staging) or a move to ECS is a code change.

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

### D-4. GitHub Actions for CI/CD, images in ECR

- **CI and builds:** GitHub Actions, free for public repos. The image is built on GitHub's
  **arm64 runner** (`ubuntu-24.04-arm`, free for public repos), so there's no slow emulation.
- **Registry: ECR**, a private repository `asktravel-api`:
  - The workflow pushes through the OIDC deploy role (D-5); no stored credentials.
  - The server pulls through its instance role and the ECR credential helper; no `docker login`
    secrets either.
  - **Lifecycle policy:** keep the last 15 images, so storage stays at a few cents.
  - **Scan on push** (basic scanning, free) lists known vulnerabilities in each image.
- **The image contains:** code and dependencies only. No secrets, no guidebooks.
- **Open WebUI, Caddy and Postgres** still come from their public registries (ghcr.io, Docker
  Hub). Only our image is in ECR.
- **Not chosen:** GHCR (the previous choice). It's free and simpler, but it skips IAM-based
  registry access, which is the lesson here.

### D-5. Deploys through OIDC and SSM Run Command, with an approval gate

**How a deploy runs:**

1. After CI passes on `main`, the deploy job waits in GitHub's `production` environment until
   you approve it. Required reviewers are free on public repos.
2. It asks GitHub for a short-lived **OIDC token** and exchanges it for temporary credentials of
   the IAM role `asktravel-github-deploy`. The role's trust policy accepts only this repo's
   `production` environment.
3. It pushes the image to ECR, then calls `aws ssm send-command` with the `AWS-RunShellScript`
   document, targeting the instance by its tag. The SSM agent on the server runs
   `deploy/deploy.sh <sha>`.
4. It waits for the command's result (`aws ssm wait command-executed`) and checks
   `https://asktravel.<domain>/agent-health` from the runner.

**Why this way:**

- **No SSH anywhere.** Port 22 is closed and the instance has no key pair. The SSM agent makes
  an outbound connection to AWS, so nothing listens for inbound shells.
- **No long-lived secrets in GitHub.** The role ARN isn't secret, and the credentials expire
  within the hour.
- **Audit for free.** CloudTrail records every `SendCommand` and every Session Manager login.
- You see each deploy in the GitHub UI, approve it with one click, and roll back by re-running
  an old commit's deploy.

**Not chosen:**

- **Tailscale + SSH** (the previous choice): simpler, but it skips IAM federation and SSM.
- **Pull-based auto-updaters** (a container on the server that watches the registry): no
  approval step and no visibility.
- **AWS CodeDeploy / CodePipeline:** more services to learn at once for one server. A good later
  lesson.

### D-6. Caddy for HTTPS

- **Why:** it gets and renews Let's Encrypt certificates automatically from a 3-line config,
  streams responses without buffering, and uses ~30 MB.
- **Not chosen:**
  - Nginx + certbot: more configuration to learn at once.
  - An Application Load Balancer with a free ACM certificate: ~$16–20/month, more than the
    server. It's the first thing to add when you go to two servers.

### D-7. No staging server

- **Why:** a second server doubles the cost.
- **Instead:** "staging" is the production compose file running on your laptop (D3's acceptance
  test), plus the approval gate.
- **Revisit with:** more users or contributors. With infrastructure as code (D-2's graduation
  path), a `t4g.micro` staging instance and a `staging` environment in the same workflow are
  small additions.

### D-8. Secrets in SSM Parameter Store

- **Where they live:** `SecureString` parameters under `/asktravel/prod/` (for example
  `/asktravel/prod/OPENROUTER_API_KEY`), encrypted with the AWS-managed KMS key. The standard
  tier is free.
- **How the app gets them:** `deploy.sh` reads `/asktravel/prod/*` with the instance role and
  writes `/opt/asktravel/.env` (`chmod 600`, owned by root) just before `docker compose up`. To
  rotate a secret, change the parameter and redeploy.
- **Who can read them:** only the instance role (`ssm:GetParametersByPath` on that path) and
  your admin login. GitHub holds nothing but the deploy role's ARN.
- **Not chosen:** AWS Secrets Manager ($0.40 per secret per month). Its automatic rotation
  helps RDS passwords, which we don't have.

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
- **Built for `linux/arm64`** (the t4g server is Graviton). Your Apple-silicon laptop is arm64
  too, so local builds already match. D0 checks that every dependency has arm64 Linux wheels
  (Playwright's Chromium, `psycopg`, `fast-flights`' `primp`).

### 5.2 `docker-compose.prod.yml`

Differences from the dev file:

- **Tracing:** LangSmith settings in `.env`; nothing to run.
- **Pinned versions:**
  - Open WebUI is pinned to a release (e.g. `ghcr.io/open-webui/open-webui:v0.12.4`), not
    `:main`. Upgrades are their own PR, followed by `e2e_chat_deletion.py`.
  - `api` uses `<account>.dkr.ecr.us-east-1.amazonaws.com/asktravel-api:${IMAGE_TAG}`.
- **Published ports:** only Caddy's 80/443; nothing else is published.
- **Services:**
  - **`api`** mounts `./guidebooks:/app/data/guidebooks:ro`, has `stop_grace_period: 60s` (lets
    a running turn finish), and `restart: unless-stopped`.
  - **`caddy`** (`caddy:2`) has volumes for its certificates and its config file
    (`deploy/Caddyfile`).
  - **`postgres`**: a strong password from `.env` and `restart: unless-stopped`.
- **Logs:** the `awslogs` driver sends every container's log to a CloudWatch Logs group
  `/asktravel/prod` (14-day retention). The instance role allows it; no keys needed.
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
- `bootstrap.sh`: turns a fresh Ubuntu server into an AskTravel host. It runs once as the
  instance's **user data** at launch, and it's idempotent, so re-running it is safe. Its steps:
  - Install Docker from Docker's own repository, the AWS CLI v2 (arm64) and the
    `amazon-ecr-credential-helper` (so `docker pull` from ECR uses the instance role).
  - Turn on automatic security updates (`unattended-upgrades`).
  - Add 2 GB of swap.
  - Check that the SSM agent is running (it's preinstalled on Ubuntu AMIs).
  - Create `/opt/asktravel` and clone the repo there.
  - Install the nightly backup timer.

  Committing it is how this one server becomes reproducible: "infrastructure as code, lite".
- `deploy.sh <sha>` runs on the server for every deploy (through SSM Run Command):
  1. `git fetch && git checkout <sha>`, to get the compose file, Caddyfile and branding.
  2. Write `.env` from Parameter Store (`aws ssm get-parameters-by-path --with-decryption`).
  3. A quick `pg_dump`, a safety net just before the change.
  4. `IMAGE_TAG=<sha> docker compose pull && docker compose up -d --wait`.
  5. Health checks: `curl -fsS localhost/agent-health`, then Open WebUI's `/health`.
  6. If either fails, redeploy the previous good tag (in `.last-good`) and exit non-zero, so
     the SSM command, and with it the workflow, fails red.
  7. Record `<sha>` in `.last-good`; `docker image prune -f`.
- `backup.sh`: `pg_dump -Fc` → `aws s3 cp` to `s3://asktravel-backups-<id>/daily/<date>.dump`
  (the instance role allows `s3:PutObject` there and nothing else), then pings a
  Healthchecks.io URL (it alerts if a night is missed).
- `runbook.md`: how to deploy, roll back, open a shell (Session Manager), restore from S3 or from
  an EBS snapshot, read logs (CloudWatch Logs Insights), rotate secrets (Parameter Store),
  resize the instance, and handle a full disk.

## 6. AWS and server setup (done by hand once, written down in the runbook)

Do it in the console first, so you see each piece. §3 D-2's graduation path captures it as
code later.

1. **Account hygiene** (30 min, before anything else):
   - **Root user:** MFA on, never used again.
   - **Your everyday login:** an admin user through IAM Identity Center, with MFA. Use the AWS CLI
     with `aws sso login`, not access keys.
   - **Budgets:** an AWS Budget of $25/month with email alerts at 50%, 80% and 100% (actual) and at
     100% (forecast). Turn on Cost Anomaly Detection (free).
   - **Free plan:** if the account is on the 6-month free plan, upgrade it to the paid plan before
     the beta. When a free-plan account reaches 6 months or uses up its credits, AWS closes it.
2. **Network (default VPC, us-east-1):**
   - Use the default VPC and one of its public subnets. Look at its route table: `0.0.0.0/0` →
     internet gateway is what makes the subnet "public".
   - **Security group `asktravel-web`:** inbound TCP 80 and 443 from `0.0.0.0/0` and `::/0`.
     Nothing else inbound. Outbound: all (the default).
3. **IAM:**
   - **Instance role `asktravel-ec2`** (with its instance profile):
     - `AmazonSSMManagedInstanceCore` (Session Manager and Run Command).
     - ECR pull on `asktravel-api` only.
     - `s3:PutObject` on `arn:aws:s3:::asktravel-backups-…/daily/*` only.
     - `ssm:GetParametersByPath` on `/asktravel/prod/*` (+ `kms:Decrypt` through the AWS-managed key).
     - CloudWatch Logs write to `/asktravel/prod`, and the CloudWatch agent policy.
   - **GitHub OIDC:** add the identity provider `token.actions.githubusercontent.com`, then the role
     `asktravel-github-deploy`:
     - Trust policy: `sub` = `repo:stoicdataguy/asktravel-agent-harness:environment:production`.
     - Permissions: push to the ECR repo; `ssm:SendCommand` with `AWS-RunShellScript` on
       instances tagged `app=asktravel`; read command results.
4. **ECR:** private repository `asktravel-api`, scan on push, lifecycle policy "keep the last 15
   images".
5. **EC2:**
   - **AMI:** Ubuntu 24.04 LTS, arm64. **Type:** `t4g.small` (or `t4g.medium` after D0), credit
     specification `standard`.
   - **No key pair.** Security group `asktravel-web`, instance profile `asktravel-ec2`, tag
     `app=asktravel`.
   - **Storage:** 20 GB gp3, encrypted, "delete on termination" off (the data survives a mistake).
   - **Metadata:** IMDSv2 required (hop limit 2, so containers can use the role if needed).
   - **User data:** `deploy/bootstrap.sh`.
   - **Elastic IP:** allocate one and associate it.
   - Check: `aws ssm start-session --target <instance-id>` gives you a shell.
6. **Domain and DNS:**
   - Register a domain in Route 53 (~$14/year for `.com`), which creates a hosted zone
     ($0.50/month). A domain registered elsewhere can point its name servers to the zone.
   - `A` record `asktravel` → the Elastic IP.
7. **S3 for backups:**
   - **Bucket:** `asktravel-backups-<random>`, Block Public Access on (the default),
     versioning on, default encryption on.
   - **Lifecycle rule:** delete after 35 days.
8. **Snapshots and monitoring:**
   - **Data Lifecycle Manager:** a policy for volumes tagged `app=asktravel`: a daily snapshot,
     keep 7.
   - **SNS topic** `asktravel-alerts` with your email subscribed.
   - **CloudWatch alarms** to that topic (the first 10 alarms are free):
     - `StatusCheckFailed_System` → also the **recover** action (AWS moves the instance to healthy
       hardware).
     - `CPUCreditBalance` < 30.
     - Memory used > 90% and disk used > 80% (CloudWatch agent metrics).
   - **CloudWatch Logs:** group `/asktravel/prod`, retention 14 days.
9. **Secrets:** create the `/asktravel/prod/*` `SecureString` parameters (the keys below).
10. **Server:** in a Session Manager shell, check that `bootstrap.sh` finished. Copy the guidebooks
    up through S3 (`aws s3 cp` from your laptop, then down on the server); they must never go in
    the image.
11. **External accounts:**
    - **OpenRouter:** a *separate* production key with a credit limit, so a bug or abuse can't
      drain your account.
    - **LangSmith:** a project `asktravel-prod` and a tracing key for the server.
    - **Monitoring:** UptimeRobot (free) on `https://asktravel.<domain>/agent-health`, alerting
      your email; Healthchecks.io (free) for the backup job.

Production settings, as Parameter Store entries under `/asktravel/prod/` (also listed in
`.env.example`, commented):

```
OPENROUTER_API_KEY=...       # the production key with a credit limit
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
| EC2 `t4g.small`, on demand ($0.0168/h) | $12.26 (`t4g.medium`: $24.53) |
| Public IPv4 address (Elastic IP, $0.005/h) | $3.65 |
| EBS gp3, 20 GB ($0.08/GB) | $1.60 |
| EBS snapshots (7 daily, incremental) | ~$0.50 |
| Route 53 hosted zone | $0.50 |
| Domain (~$14/year for `.com`) | ~$1.15 |
| ECR (~2 GB of images) | ~$0.20 |
| S3 backups (~35 dumps, well under 5 GB) | < $0.25 |
| CloudWatch (2 agent metrics; alarms and logs within the free tier) | ~$0.60 |
| SSM, Parameter Store (standard), IAM, OIDC, DLM, Budgets, CloudTrail (90-day history) | $0 |
| GitHub Actions (incl. arm64 runners) on a public repo, UptimeRobot, Healthchecks.io | $0 |
| LangSmith Developer | $0 (pay per extra trace over 5k a month) |
| **Hosting total** | **~$21** (4 GB: ~$33) |
| OpenRouter (LLM) | **Usage-based; likely more than hosting.** Read your real cost per chat from OpenRouter's activity page or LangSmith, multiply by expected chats, and set the prod key's credit limit to that plus a margin |

Prices are for us-east-1 from the AWS and LangSmith pricing pages, checked 2026-10-30; recheck
them in the AWS Pricing Calculator before D4. Data transfer out is free up to 100 GB a month,
far more than the beta needs.

**Free credits:** accounts created since July 2025 get $100 in credits at sign-up, and up to
$100 more for trying services (one of them is launching an EC2 instance). That can cover the
first months, but the free plan ends after 6 months and closes the account unless you upgrade
(§6.1). Plan the budget without the credits.

## 8. Phases

Each phase ends with checks you can run. They follow the repo's workflow: issue → branch →
PR → approval → merge.

### D0. Decide and spike (1–2 evenings, ~$0.20) — #90

1. **Decide the blockers in §4.** Guidebooks: on, off or replaced. Beta size or branding.
2. **Set up the account (§6.1)**, including budgets.
3. **Measure the real server.**
   - Launch a throwaway `t4g.small` (Ubuntu 24.04 arm64) in us-east-1 with an SSM-enabled role
     and no key pair. Connect with Session Manager.
   - Build the API image for arm64 on it, or on your Mac. Install Playwright's Chromium.
   - Run `fetch_hotel_cards` twice in parallel and once alone, plus a `search_flights` call.
     Watch `free -m` and `docker stats`.
   - Terminate the instance and release its IP the same day. Check the billing page next day.
4. **Check Google from an AWS IP.** Do the flight and hotel results come back, or is there a
   captcha or consent page?

**Exit checks**

- [ ] Every dependency installs and runs on arm64 Linux (otherwise choose `t3.small`).
- [ ] Scrapers work from AWS (or the fallback is decided).
- [ ] Peak memory with one browser fits under ~1.7 GB, so 2 GB will do; otherwise choose 4 GB.
- [ ] Budget alerts are live, and nothing from the spike is still billing.

### D1. API auth and invite-only signup — #27

- [ ] Requests without the token get 401.
- [ ] `e2e_branching.py` passes.
- [ ] A new sign-up lands in "pending" until approved.

### D2. CI — #29

- [ ] A PR with a failing test shows a red check and can't be merged.
- [ ] A clean PR goes green in under ~3 min.

### D3. Containerise and run prod compose locally — #28 + memory diet

- [ ] `docker compose -f docker-compose.prod.yml up` on your laptop, with Caddy on
  `localhost`, serves the full app (the `awslogs` driver swapped for `json-file` locally).
- [ ] `e2e_progress.py`, `e2e_branching.py`, `e2e_memory.py` and `e2e_hotels.py` pass against it.
- [ ] Two parallel hotel searches run one after the other and both succeed.

### D4. AWS setup and first deploy, by hand — #30

Do every step yourself once before automating it: the best way to learn what the pipeline does.

1. Set up AWS (§6.2–6.11).
2. Push an image to ECR from your laptop (`aws ecr get-login-password | docker login …`).
3. In a Session Manager shell: run `deploy.sh <sha>` by hand.
4. Create your admin account first, while signup is still in your hands.

**Exit checks**

- [ ] `https://asktravel.<domain>` loads with a valid certificate.
- [ ] Ports 8000, 5432 and 22 are closed from outside (`nc -vz <ip> 22` fails).
- [ ] The instance has no key pair, and Session Manager gives you a shell.
- [ ] Container logs show up in CloudWatch Logs.
- [ ] `e2e_flights.py` and `e2e_hotels.py` pass when run on the server. They need small
  `--base-url` / `--api` options added.

### D5. Continuous deployment — #91

`.github/workflows/deploy.yml`:

```yaml
on:
  push: { branches: [main] }        # i.e. a PR was merged
  workflow_dispatch:                 # manual: redeploy any commit (rollback)
    inputs: { sha: { required: false } }
permissions: { id-token: write, contents: read }   # id-token: needed for OIDC
concurrency: { group: production, cancel-in-progress: false }
jobs:
  test:    # same steps as ci.yml (reuse it with `uses: ./.github/workflows/ci.yml`)
  deploy:  # needs: test; runs-on: ubuntu-24.04-arm; environment: production (you approve)
           # aws-actions/configure-aws-credentials (role-to-assume: asktravel-github-deploy)
           # aws-actions/amazon-ecr-login → docker build/push <account>.dkr.ecr…/asktravel-api:<sha>
           # aws ssm send-command --targets Key=tag:app,Values=asktravel
           #   --document-name AWS-RunShellScript
           #   --parameters 'commands=["/opt/asktravel/deploy/deploy.sh <sha>"]'
           # aws ssm wait command-executed …; then curl -fsS https://asktravel.<domain>/agent-health
```

**Exit checks**

- [ ] Merging a PR that changes a prompt shows up in prod after one approval click, with no
  terminal.
- [ ] A deliberately broken image (failing health check) is rolled back automatically, and the
  run goes red.
- [ ] `workflow_dispatch` with an older SHA rolls back.
- [ ] The repo has no AWS secrets: Settings → Secrets lists none, only the role ARN as a
  variable.
- [ ] CloudTrail shows the deploy's `SendCommand`, made by the `asktravel-github-deploy` role.

### D6. Backups, restore drill, monitoring, limits — #31, #32

- [ ] The nightly backup appears in S3, and a missed night alerts Healthchecks.io.
- [ ] Daily EBS snapshots appear, and the 8th oldest is deleted.
- [ ] **Restore drill:** a dump restored into a scratch Postgres on your laptop. The notebook
  shows the same users, facts and threads. Write down how long it took: your recovery time.
- [ ] **Snapshot drill:** a volume created from a snapshot, attached to a scratch instance,
  mounts and has the data. Then delete both.
- [ ] UptimeRobot emails you within 5 min of `docker compose stop api`.
- [ ] A test alarm (`aws cloudwatch set-alarm-state`) reaches your email through SNS.
- [ ] The 41st turn of the day gets the "daily limit reached" message.

### D7. Beta launch — #92

1. **Waves:** 5 friends → 25 → 50 (→ 100 once §4.2 and §4.4 are resolved).
2. **Invite note:** what AskTravel is, that conversations are stored and traced, how to delete
   your data, and where to send feedback (👍/👎 in the chat; the Feedback phase F1–F4 makes those
   useful).
3. **Weekly 15-minute review:**
   - The CloudWatch dashboard: CPU credits, memory, disk.
   - AWS Cost Explorer (by service) and OpenRouter spend.
   - LangSmith trace count (against 5k a month) and errored runs.
   - The `⏳` retry counts in the API logs (CloudWatch Logs Insights, #51).

**Exit checks**

- [ ] A week at 25 users with no unplanned downtime longer than 10 min.
- [ ] Spend within the forecast.

## 9. Issues

| Issue | Phase | Points |
|---|---|---|
| #90 Prod D0: spike, arm64 check, server size and Google scraping from AWS | D0 | 1 |
| #91 Prod D5: continuous deployment (OIDC, ECR, SSM Run Command, rollback) | D5 | 3 |
| #92 Prod D7: beta launch, invite waves, privacy note, weekly review | D7 | 1 |
| Not filed yet: guidebook switch (only if D0 decides "off" in §4.1) | D3 | 1 |

The existing #27–#32 keep their scope. #30 becomes "AWS setup and first deploy by hand" (§6),
with the server size decided in D0. #14's ECS design stays in PLAN.md as the path after the
beta.

## 10. What you'll have learned by the end

- **AWS core:** regions and availability zones, VPCs, subnets and route tables, internet
  gateways, security groups, AMIs, instance types and CPU credits, EBS volumes and snapshots,
  Elastic IPs, user data, IMDSv2.
- **AWS identity:** IAM users vs roles, instance profiles, least-privilege policies, trust
  policies, OIDC federation from GitHub, IAM Identity Center, CloudTrail.
- **AWS operations:** SSM Session Manager, Run Command and Parameter Store; ECR; Route 53;
  CloudWatch metrics, alarms, agent and Logs Insights; SNS; Data Lifecycle Manager; Budgets and
  Cost Explorer.
- **Containers in production:** images vs containers, registries, tags vs digests, multi-arch
  builds, health checks, restart policies, resource limits, log drivers.
- **CI/CD:** workflows, runners, jobs and dependencies, environments and approvals,
  concurrency, artifacts (images) promoted by commit SHA, automatic rollback.
- **Operations:** backups that are tested, uptime and job monitoring, runbooks, rate and spend
  limits, upgrade discipline (pinned versions).
- **What to change at the next level:** infrastructure as code, an Application Load Balancer
  with two instances, RDS, ECS, staging. Each one now builds on something you've already set up
  by hand.
