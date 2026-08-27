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
  somewhere your cluster can pull from - see "Build and push the image"
  below. This example uses `cremerfc/ai-agent-gongy` (public on Docker Hub),
  pinned by digest in `deployment.yaml` - swap `image:` if you build and
  push your own.
- An agent registered in AgentOps (the Add Agent wizard, framework
  "Something else") with a minted worker token. Its `agent_id` must match
  `AGENTOPS_AGENT_ID` in `deployment.yaml` (currently `fernandos-outside-agent`)
  and `agent-spec.yaml`'s `agent_id`.
- `GONG_ACCESS_KEY`, `GONG_ACCESS_KEY_SECRET`, and `ANTHROPIC_API_KEY` bound
  as credentials on that agent in AgentOps (Settings → Agents). The handler
  pulls these per run via `apply_secret_to_env` - they're never stored in the
  cluster.

## Build and push the image

`.github/workflows/build-and-push.yml` builds and pushes the image
automatically on every push to `main` (or on demand via the Actions tab's
"Run workflow" button) - `linux/amd64` only, matching GKE's standard node
pools, so there's no local architecture pitfall to worry about. It needs two
repo secrets (Settings -> Secrets and variables -> Actions):

- `DOCKERHUB_USERNAME` - a Docker Hub username with push access to the image
- `DOCKERHUB_TOKEN` - a Docker Hub access token (not your account password)

Each run's job summary prints the resulting `image@sha256:...` digest -
paste that into `deployment.yaml`'s `image:` field (replacing
`REPLACE_WITH_DIGEST_FROM_CI` on first setup) and `kubectl apply -f k8s/` (or
`rollout restart`, see "Updating" below) to deploy it. Pinning by digest
instead of `:latest` means the manifest always names the exact image that
was tested/reviewed, and a compromised or overwritten `:latest` tag on
Docker Hub can't silently change what's running in the cluster.

For a local/manual build instead of CI - e.g. testing a change before it's
merged - `docker build` targets your local machine's architecture by
default, so building on Apple Silicon (or any arm64 machine) produces an
`arm64` image that fails on GKE nodes with an `exec format error` /
`CrashLoopBackOff`. Use `buildx` and target `linux/amd64` explicitly (add
`,linux/arm64` too if you also want to run the same image tag locally on an
Apple Silicon machine):

```sh
docker buildx build --platform linux/amd64 -t cremerfc/ai-agent-gongy:latest --push .
```

`--push` publishes directly (a multi-platform build can't be `docker load`ed
into the local daemon, so there's no separate `docker push` step). Swap the
tag for your own registry if you're not using `cremerfc/ai-agent-gongy`. A
manual push like this still lands on the mutable `:latest` tag - grab its
digest from Docker Hub (or `docker inspect --format '{{index .RepoDigests 0}}'
cremerfc/ai-agent-gongy:latest` right after the push) if you want to deploy
it the same pinned way.

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

The Deployment is pinned to an image digest (`imagePullPolicy: IfNotPresent`),
so a plain `rollout restart` won't pick up a new build on its own - after
CI pushes a new image, edit `deployment.yaml`'s `image:` to the digest from
that run's job summary, then re-apply:

```sh
kubectl apply -f k8s/deployment.yaml
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
