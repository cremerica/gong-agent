# Deploying to GKE

This is a from-scratch (non-Claude-Code) [AgentOps](https://agentops.komodor.com/docs/agents/building-your-own-agent)
worker, so it deploys as a plain Kubernetes `Deployment` running the image
built from the repo's `Dockerfile` - not the `agentops-agent-base` helm chart,
which is a config-driven *Claude Code* worker (prompt/skills/subagents) and
has no way to run custom code like `gong_agent/`. This directory is meant as
a self-contained example of that "bring your own image" path.

## Prerequisites

- A GKE cluster and a `kubectl` context pointed at it (`gcloud container
  clusters get-credentials <cluster> --zone <zone> --project <project>`).
- The worker image, already built from this repo's `Dockerfile` and pushed
  somewhere your cluster can pull from. This example uses
  `cremerfc/ai-agent-gongy:latest` (public on Docker Hub) - swap
  `image:` in `deployment.yaml` if you build and push your own.
- An agent registered in AgentOps (the Add Agent wizard, framework
  "Something else") with a minted worker token. Its `agent_id` must match
  `AGENTOPS_AGENT_ID` in `deployment.yaml` (currently `fernandos-outside-agent`)
  and `agent-spec.yaml`'s `agent_id`.
- `GONG_ACCESS_KEY`, `GONG_ACCESS_KEY_SECRET`, and `ANTHROPIC_API_KEY` bound
  as credentials on that agent in AgentOps (Settings → Agents). The handler
  pulls these per run via `apply_secret_to_env` - they're never stored in the
  cluster.

## Build and push the image

GKE's standard node pools run `linux/amd64`. `docker build` targets your
local machine's architecture by default, so building on Apple Silicon (or
any arm64 machine) produces an `arm64` image that fails on those nodes with
an `exec format error` / `CrashLoopBackOff` - it's not a Kubernetes config
issue, the binary just can't run on the node's CPU architecture. Build with
`buildx` and target `linux/amd64` explicitly (add `,linux/arm64` too if you
also want to run the same image tag locally on an Apple Silicon machine):

```sh
docker buildx build --platform linux/amd64 -t cremerfc/ai-agent-gongy:latest --push .
```

`--push` publishes directly (a multi-platform build can't be `docker load`ed
into the local daemon, so there's no separate `docker push` step). Swap the
tag for your own registry if you're not using `cremerfc/ai-agent-gongy`.

## Deploy

```sh
# 1. Namespace
kubectl apply -f k8s/namespace.yaml

# 2. Worker token, created imperatively so it's never written to a file
#    (and never committed to git) - paste the token from the Add Agent
#    wizard / Settings → Agents → Worker tokens.
kubectl create secret generic gong-poc-health-worker-token \
  --from-literal=token='<your AGENTOPS_WORKER_TOKEN>' \
  -n gong-poc-health

# 3. Everything else
kubectl apply -f k8s/
```

## Verify

```sh
kubectl -n gong-poc-health get pods
kubectl -n gong-poc-health logs -l app=gong-poc-health-worker -f
```

The pod should log that it registered and is heartbeating; the agent should
then show healthy/live in AgentOps' Fleet view. From there its
`weekly-poc-health` cron trigger (declared in `agent-spec.yaml`) fires on
schedule, and it can be invoked on demand from Fleet or Chat.

## Updating

The Deployment pulls `:latest` on every restart (`imagePullPolicy: Always`),
so after pushing a new image:

```sh
kubectl -n gong-poc-health rollout restart deployment/gong-poc-health-worker
```

## Rotating the worker token

Rotating in AgentOps invalidates the old token immediately - update the
Secret and restart the Deployment right after, not as two separate sessions:

```sh
kubectl -n gong-poc-health delete secret gong-poc-health-worker-token
kubectl create secret generic gong-poc-health-worker-token \
  --from-literal=token='<new token>' -n gong-poc-health
kubectl -n gong-poc-health rollout restart deployment/gong-poc-health-worker
```
