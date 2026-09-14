#!/usr/bin/env bash
# AitherSwarm CI proof — parallel awdk agents work a shared goal on
# canonical fleet infra (MicroScheduler LLM, vLLM nomic-768 embeddings, the
# tenant Qdrant dataplane, and the AitherFlux context superhighway), then a
# reasoning synthesizer assembles the final answer.
#
# Runs INSIDE a container on the fleet network so service names resolve and the
# AitherNet internal CA verifies. Invoked by .github/workflows/adk-swarm-proof.yml.
#
# Env (with sane defaults):
#   SWARM_GOAL     the shared goal
#   SWARM_TASKS    how many subtasks to decompose into      (default 4)
#   SWARM_WORKERS  how many CONCURRENT worker agents        (default 3)
#   AITHER_API_KEY passed through for MicroScheduler auth
# Canonical endpoints are set below; override via the environment if needed.
set -euo pipefail

ADK_SRC=${ADK_SRC:-/adk}
CERTS=${CERTS:-/certs/aithernet-ca-bundle.pem}
SWARM_GOAL=${SWARM_GOAL:-"Summarize the security tradeoffs of connecting a remote CI runner to a private fleet."}
SWARM_TASKS=${SWARM_TASKS:-4}
SWARM_WORKERS=${SWARM_WORKERS:-3}
OUT=${OUT:-/tmp/final.json}

echo "== install awdk =="
pip install -q "$ADK_SRC" 2>&1 | tail -2 || { echo "pip install failed"; exit 1; }

# Trust the AitherNet internal CA for every https client.
CERTIFI=$(python -c "import certifi; print(certifi.where())")
cat "$CERTS" >> "$CERTIFI"
export SSL_CERT_FILE="$CERTIFI"
export AITHER_CA_BUNDLE="$CERTS"

# Canonical fleet endpoints (service names over the fleet network / mesh).
export AITHER_TENANT_ID=${AITHER_TENANT_ID:-swarmproof}
export AITHER_LLM_BACKEND=${AITHER_LLM_BACKEND:-gateway}
export AITHER_GATEWAY_URL=${AITHER_GATEWAY_URL:-https://aitheros-microscheduler:8150}
export AITHER_EMBEDDINGS_URL=${AITHER_EMBEDDINGS_URL:-https://aither-vllm-embeddings:8209}
export AITHER_FLEET_QDRANT_URL=${AITHER_FLEET_QDRANT_URL:-http://aitheros-qdrant:6333}
export AITHER_FLUX_URL=${AITHER_FLUX_URL:-https://aitheros-flux:8117}
export AITHER_GRAPH_EMBEDDER=adk
export AITHER_LOG_LEVEL=${AITHER_LOG_LEVEL:-INFO}

echo; echo "== PLAN =="
python -m adk.swarm plan --goal "$SWARM_GOAL" --tasks "$SWARM_TASKS" --effort 6

echo; echo "== $SWARM_WORKERS CONCURRENT WORKERS =="
pids=()
for i in $(seq 1 "$SWARM_WORKERS"); do
  AITHER_DATA_DIR="/tmp/aither/w$i" \
    python -m adk.swarm worker --goal "$SWARM_GOAL" --agent "worker-$i" \
    --effort 5 --max-tasks "$SWARM_TASKS" > "/tmp/w$i.json" 2>"/tmp/w$i.log" &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done
for i in $(seq 1 "$SWARM_WORKERS"); do
  echo -n "worker-$i "; python -c "import json;d=json.load(open('/tmp/w$i.json'));print('claimed', [f['index'] for f in d['findings']])" 2>/dev/null || echo "(see /tmp/w$i.log)"
done

echo; echo "== SYNTHESIZE =="
AITHER_DATA_DIR=/tmp/aither/synth python -m adk.swarm synthesize --goal "$SWARM_GOAL" --effort 8 --out "$OUT"

echo; echo "== RESULT =="
python -c "import json;d=json.load(open('$OUT'));print('plane:',d.get('source_plane'));print('findings_used:',d['findings_used']);print('agents:',d['agents']);print();print(d['answer'])"
