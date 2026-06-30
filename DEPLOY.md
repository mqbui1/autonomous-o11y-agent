# End-to-End Deployment Guide

Deploy the full stack on a single EC2 instance:
- **OpenTelemetry Astronomy Shop** — 20-service demo app generating realistic traces, metrics, and logs
- **Splunk OTel Collector** (gateway mode) — receives all telemetry, exports to Splunk Observability Cloud
- **O11y Agent** (streaming mode) — co-deployed alongside the gateway, receiving a fan-out copy of all telemetry

---

## Architecture

```
EC2 Instance
├── k3d cluster (k3s in Docker)
│   ├── astronomy-shop namespace
│   │   └── OpenTelemetry Demo (20 services: Go, Python, Node, Java, .NET, Ruby, Rust, PHP)
│   │       └── Built-in OTel Collector → gateway:4317
│   │
│   └── monitoring namespace
│       ├── Splunk OTel Collector (gateway)
│       │   ├── ← OTLP:4317 from astronomy-shop
│       │   ├── → Splunk Observability Cloud (signalfx + HEC)
│       │   └── → O11y Agent :4318 (encoding=json, retry_on_failure=false)
│       │
│       └── O11y Agent (streaming mode)
│           ├── OTLP/HTTP receiver :4318
│           ├── Real-time: PII scanner, attribute checker, cardinality tracker, service tracker
│           ├── ObservationBuffer → batch assessments (every 60 min)
│           └── Batch: 5-specialist parallel assessment → Splunk O11y API
```

---

## EC2 Requirements

| Spec | Minimum | Recommended |
|---|---|---|
| Instance type | t3.large (2 CPU, 8 GB) | t3.xlarge (4 CPU, 16 GB) |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Disk | 30 GB | 50 GB |
| Ports open | 22 (SSH), 8080 (shop UI) | 22, 8080, 4317, 4318 |

---

## Prerequisites

On your local machine, clone all required repositories as siblings:

```bash
cd ~/Documents   # or wherever you keep repos

git clone https://github.com/mqbui1/autonomous-o11y-agent
git clone https://github.com/mqbui1/auto-detector-provisioner
git clone https://github.com/mqbui1/o11y-usage-governance
git clone https://github.com/mqbui1/o11y-instrumentation-analyzer
git clone https://github.com/mqbui1/splunk-o11y-health-check
```

You will need:
- **Splunk Observability Cloud** access token with `API`, `INGEST` scope
- **AWS credentials** with `AmazonBedrockFullAccess` (for Claude via Bedrock)
- AWS Bedrock enabled in `us-west-2` with Claude Sonnet 4.6 model access

---

## Deployment

### Option A — Automated (one command)

```bash
cd autonomous-o11y-agent

export SPLUNK_ACCESS_TOKEN=<your-splunk-token>
export SPLUNK_REALM=us1
export AWS_ACCESS_KEY_ID=<your-aws-key>
export AWS_SECRET_ACCESS_KEY=<your-aws-secret>
export AWS_DEFAULT_REGION=us-west-2
export SPLUNK_ENVIRONMENT=astronomy-shop-demo

chmod +x deploy/*.sh
./deploy/deploy.sh
```

This runs all 5 steps in order and finishes with a status summary.

---

### Option B — Step by step

#### Step 1: Setup EC2

```bash
# SSH into your EC2 instance
ssh ubuntu@<EC2_IP>

# Clone repos
cd ~
git clone https://github.com/mqbui1/autonomous-o11y-agent
git clone https://github.com/mqbui1/auto-detector-provisioner
git clone https://github.com/mqbui1/o11y-usage-governance
git clone https://github.com/mqbui1/o11y-instrumentation-analyzer
git clone https://github.com/mqbui1/splunk-o11y-health-check

# Install tools + create k3d cluster
export CLUSTER_NAME=o11y-demo
chmod +x autonomous-o11y-agent/deploy/*.sh
sudo -E autonomous-o11y-agent/deploy/01-setup.sh
```

#### Step 2: Build the agent image

```bash
export AGENT_IMAGE=o11y-agent:latest
export CLUSTER_NAME=o11y-demo
autonomous-o11y-agent/deploy/02-build.sh
```

The Dockerfile builds from the parent directory so all sibling tool projects are bundled into the image.

#### Step 3: Deploy Splunk OTel Collector

```bash
export SPLUNK_ACCESS_TOKEN=<your-token>
export SPLUNK_REALM=us1
export CLUSTER_NAME=o11y-demo
autonomous-o11y-agent/deploy/03-deploy-collector.sh
```

Deploys the Splunk OTel Collector in **gateway mode** into the `monitoring` namespace.

#### Step 4: Deploy Astronomy Shop

```bash
export SPLUNK_ENVIRONMENT=astronomy-shop-demo
autonomous-o11y-agent/deploy/04-deploy-astronomy-shop.sh
```

Deploys 20+ services from the OpenTelemetry Demo. Takes 3–5 minutes for all pods to start.

All services send telemetry (traces, metrics, logs) via their built-in OTel Collector to the Splunk gateway at `splunk-otel-collector-gateway.monitoring:4317`.

#### Step 5: Deploy O11y Agent + patch collector

```bash
export AWS_ACCESS_KEY_ID=<your-key>
export AWS_SECRET_ACCESS_KEY=<your-secret>
autonomous-o11y-agent/deploy/05-deploy-agent.sh
```

This:
1. Deploys the o11y-agent in streaming mode
2. Reads the auto-generated `o11y-agent-gateway-patch` ConfigMap
3. Applies it to the Splunk collector — adds the `otlp/o11y_agent` exporter with `encoding: json` and `retry_on_failure: false`

After this step, the gateway fans every trace and metric to the agent's OTLP receiver at port 4318.

---

## Verify the Deployment

### Check all pods are running

```bash
kubectl get pods -n astronomy-shop
kubectl get pods -n monitoring
```

Expected: all pods `Running` or `Completed`.

### Check the agent is receiving telemetry

```bash
# Port-forward the agent's status endpoint
kubectl port-forward svc/o11y-agent 4318:4318 -n monitoring &

curl http://localhost:4318/healthz
# → ok

curl http://localhost:4318/status
# → {"known_services": ["cartservice", "checkoutservice", ...], "top_cardinality": {...}}
```

### Check Splunk Observability Cloud

1. Open `https://app.us1.signalfx.com` (or your realm)
2. **APM > Services** — set environment filter to `astronomy-shop-demo`
3. You should see 15–20 services within 2–3 minutes of deployment
4. **Infrastructure > Kubernetes** — cluster `o11y-demo` should appear

### Watch the agent's first assessment

```bash
kubectl logs -f deployment/o11y-agent -n monitoring
```

The first batch assessment runs ~2 minutes after startup. You'll see:

```
Launching 5 specialist agents in parallel for environment=astronomy-shop-demo
Specialist 'health' complete
Specialist 'instrumentation' complete
...
```

### Browse the Astronomy Shop UI

```bash
kubectl port-forward svc/astronomy-shop-frontendproxy 8080:8080 -n astronomy-shop &
```

Open `http://localhost:8080` — browse products, add to cart, checkout. This generates realistic traces.

---

## What to Expect

See the [Expected Outcomes](#expected-outcomes) section in README.md for a full breakdown of what each agent component surfaces from the astronomy shop telemetry.

---

## Troubleshooting

**Pods stuck in Pending:**
```bash
kubectl describe pod <pod-name> -n astronomy-shop
# Usually: insufficient memory — scale up EC2 or reduce replica counts
```

**Agent not receiving telemetry (status endpoint shows empty known_services):**
```bash
# Check collector is forwarding to agent
kubectl logs deployment/splunk-otel-collector-gateway -n monitoring | grep o11y_agent
# Should see: "Exporting items" to the otlp/o11y_agent exporter
```

**Bedrock errors (AccessDeniedException):**
- Ensure the AWS credentials have `AmazonBedrockFullAccess`
- Ensure Claude Sonnet is enabled in `us-west-2` in the AWS console: Bedrock > Model access

**Assessment hangs:**
- Default specialist timeout is 900s (15 min). The first run against a fresh environment can be slow.
- Check: `kubectl logs deployment/o11y-agent -n monitoring --tail=50`

**Rebuild agent after code changes:**
```bash
docker build -t o11y-agent:latest -f autonomous-o11y-agent/Dockerfile .
k3d image import o11y-agent:latest -c o11y-demo
kubectl rollout restart deployment/o11y-agent -n monitoring
```
