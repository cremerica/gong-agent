# Gong POC Health Agent

Reads a prospect account's Gong call history and gives a Komodor SE a clear picture of where their
POC stands against the internal Rules of Engagement (ROE) playbook - what's already covered, what
might be worth a second look, and any follow-up commitments made on calls that are easy to lose
track of across a busy POC. Built as a genuinely agentic loop (manual while-loop over the raw
Anthropic Messages API) rather than a fixed extraction pipeline - the model decides which calls to
inspect, what to search for, and when it has enough evidence, up to a hard turn cap.

See `.claude/plans` (or ask Claude) for the full design rationale - Gong API constraints, why the
tool loop is manual rather than the SDK's Tool Runner, prompt caching / context editing choices, and
the deployment tradeoff analysis.

## Setup

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in GONG_ACCESS_KEY, GONG_ACCESS_KEY_SECRET, ANTHROPIC_API_KEY
```

No account list to maintain. Accounts are auto-discovered from Gong's Salesforce CRM context: every
account whose Opportunity is currently in the "Evaluation POC" stage is picked up automatically (see
`gong_agent/discovery.py`). The only optional config is `cadence_overrides.yaml`, for the rare
account whose agreed sync-call cadence isn't the 7-day default (e.g. `Telconet: 14`).

## Run

This project runs as a [Komodor AgentOps](https://agentops.komodor.com/docs/agents/building-your-own-agent)
worker (`worker.py`). The handler discovers accounts and loops over them exactly as before; two
modes, chosen by whether an SE email is given as run input:

- Manager mode (no `se_email`): every account currently in POC, org-wide.
- IC mode (`se_email` given): only accounts where that email appears as an internal call participant.

`account` optionally restricts a run to one discovered account (either mode). Both are declared as
optional fields in `agent-spec.yaml`'s `input_schema`, and can be supplied via a Chat ask (e.g. "check
CARFAX's POC health") or a manual/triggered run.

To run the handler locally without a live control-plane connection:

```sh
agentops-run-worker \
  --handler worker:handler \
  --input-json '{"account": "CARFAX"}' \
  --env-file .env \
  --output output.json --artifact-dir artifacts/
```

Each account's report lands in `--artifact-dir` as `<account-slug>/<date>.md` (same shape as the old
`reports/<account-slug>/<date>.md`), and `output.json` holds the structured summary + rolled-up text
digest the handler returns.

## Deploy

The worker dials out to the AgentOps control plane and heartbeats continuously - triggers (including
the weekly schedule, declared in `agent-spec.yaml`) are delivered over that connection, so it must run
as a long-lived service rather than a one-shot CI job.

This is a from-scratch agent (custom Gong discovery + a hand-rolled Anthropic Messages API loop, not
a Claude Code / LangChain / ADK / Agno agent), so it deploys as a **plain Kubernetes Deployment**
running the image built from the included `Dockerfile` - deliberately not the `agentops-agent-base`
helm chart, which is a config-driven *Claude Code* worker (it requires a prompt and only mounts
config into an AgentOps-authored image; there's no supported way to hand it custom code). See
[`k8s/`](k8s/) for the manifests and full deploy steps - short version:

1. Build and push the image from the included `Dockerfile` (this example uses
   `cremerfc/ai-agent-gongy:latest`).
2. Mint a worker token from the Add Agent wizard in AgentOps (framework: "Something else") and
   create it as a Secret in-cluster - see `k8s/README.md`.
3. Bind `GONG_ACCESS_KEY`, `GONG_ACCESS_KEY_SECRET`, and `ANTHROPIC_API_KEY` as credentials on the
   agent in AgentOps (Settings → Agents) - the handler pulls them per run via `apply_secret_to_env`,
   no repo, CI, or cluster secrets involved for these three.
4. `kubectl apply -f k8s/`. Once the worker is heartbeating, it's live in Fleet: its
   `weekly-poc-health` cron trigger fires on schedule, and it can also be invoked on demand from
   Fleet or Chat to test before trusting the schedule.

Reports are no longer written to the (gitignored) `reports/` dir in production - each account's
report is uploaded as a run artifact via `run.upload_artifact(...)` instead, landing wherever
AgentOps stores run artifacts. Locally, `agentops-run-worker` writes them to `--artifact-dir` (e.g.
`artifacts/`, also gitignored - see below) rather than `reports/`. The retention question this
project's reports have always carried - they contain real prospect-call quotes - still applies to
whatever holds them now; confirm what retention/access policy AgentOps applies to run artifacts
before treating that as settled long-term.

## How account discovery works

`gong_agent/discovery.py` pulls calls org-wide for a fixed lookback window (default 90 days, one
Gong API call type covers listing + parties + Salesforce context together - no per-account queries),
filters to calls whose Opportunity is in the `"Evaluation POC"` stage, and groups the result by
Salesforce Account. In IC mode, it further filters to calls where the given email appears as an
internal participant (checked client-side against the same parties data - no extra API cost). This
was validated live against Komodor's Gong tenant: an exact Opportunity-stage match correctly
recovered every real POC call for a test account, including two calls a plain participant-domain
match would have missed entirely.

There's no way to derive "the current SE" from the Gong API credentials themselves - the Access
Key/Secret is a workspace-level integration credential (confirmed: no `/v2/users/current` endpoint,
single shared workspace, no Opportunity Owner field exposed via Gong's context), so `--se-email` /
`SE_EMAIL` is the one piece of identity that has to be supplied explicitly.

## Known gaps to validate as you go

- Exact `/v2/calls/extensive` `parties`/`context` field names were confirmed against real Gong API
  responses during development (see the git history / conversation for the specific live checks) -
  if a future Gong API change alters these shapes, `gong_agent/gong_client.py::_parse_call_entry` is
  where to look.
- Discovery depends on Salesforce Opportunity stage hygiene - an account whose deal isn't promptly
  moved into "Evaluation POC" won't be discovered (or will be discovered late). This is a different
  (arguably smaller) maintenance burden than the old per-account YAML, but not zero.
- The `--se-email`/`SE_EMAIL` scoping filters on internal-party email, not Gong's `primaryUserId` -
  a live test of filtering by `primaryUserIds` returned no results even for a real user ID, so that
  server-side filter is not relied on here (inconclusive whether the field name/semantics differ, or
  the tested user is genuinely never the Gong-recorded "host" on their own calls).
