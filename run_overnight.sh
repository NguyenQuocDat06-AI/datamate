#!/usr/bin/env bash
# ==============================================================================
# DataMate Overnight Evaluation Runner (1000 Questions)
# ==============================================================================
set -u

SCRIPT_DIR="/home/ubuntu/datamate"
SOURCE_DIR="${SCRIPT_DIR}/source_test"
VENV_DIR="${SCRIPT_DIR}/venv"
RESULTS_DIR="${SCRIPT_DIR}/results"
LOGS_DIR="${SCRIPT_DIR}/logs"

mkdir -p "${RESULTS_DIR}" "${LOGS_DIR}"

START_TIME=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${LOGS_DIR}/overnight_${START_TIME}.log"
ln -sf "${LOG_FILE}" "${LOGS_DIR}/latest.log"

exec > >(tee -a "${LOG_FILE}") 2>&1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "========================================================================"
log "STARTING DATAMATE UNATTENDED OVERNIGHT 1000-EVAL"
log "Log file: ${LOG_FILE}"
log "========================================================================"

cd "${SOURCE_DIR}" || { log "ERROR: Failed to cd to ${SOURCE_DIR}"; exit 1; }

# Activate virtual environment
if [ -f "${VENV_DIR}/bin/activate" ]; then
    # shellcheck disable=SC1090
    source "${VENV_DIR}/bin/activate"
    log "Activated Python environment: $(which python) ($(python --version))"
else
    log "ERROR: Virtual environment not found at ${VENV_DIR}"
    exit 1
fi

KERNEL_SLUG="nguyenquocdat06/gemma4-12b-sql-12h-persistent-api-server"

# Function to check Kaggle session status
get_kernel_status() {
    python -c "
import sys
from scripts.kaggle_12h_client import get_authenticated_api
try:
    api = get_authenticated_api()
    status_resp = api.kernels_status('${KERNEL_SLUG}')
    st = str(getattr(status_resp, 'status', None) or status_resp).strip()
    print(st)
except Exception as e:
    print(f'ERROR: {e}')
" 2>/dev/null
}

# Function to check live tunnel ping
check_tunnel_health() {
    python -c "
import sys
from scripts.kaggle_12h_client import load_connection_config, resolve_live_base_url, ping_endpoint
try:
    cfg = load_connection_config()
    url, _ = resolve_live_base_url(cfg, max_retries=1)
    if not url:
        sys.exit(1)
    ok, code, lat, _ = ping_endpoint(url, cfg['bearer_token'])
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

push_kernel() {
    log "Pushing Kaggle GPU kernel '${KERNEL_SLUG}'..."
    kaggle kernels push -p artifacts/kaggle_12h_server
    local push_rc=$?
    log "Kernel push exited with code ${push_rc}."
    return ${push_rc}
}

wait_for_server() {
    local timeout=${1:-3600}
    log "Waiting for Kaggle server tunnel to be healthy (timeout: ${timeout}s, interval: 30s)..."
    python scripts/kaggle_12h_client.py --wait --timeout "${timeout}" --interval 30
    return $?
}

# Step 1: Initial Health Check & Launch if needed
log "Checking initial Kaggle kernel and tunnel status..."
CURRENT_STATUS=$(get_kernel_status)
log "Kaggle kernel status: ${CURRENT_STATUS}"

if check_tunnel_health; then
    log "Tunnel is already healthy and responding! Proceeding without relaunch."
else
    log "Tunnel is not healthy (Status: ${CURRENT_STATUS}). Relaunching kernel..."
    push_kernel
    if ! wait_for_server 3600; then
        log "WARNING: Server wait timed out or failed. Retrying push once more..."
        sleep 10
        push_kernel
        if ! wait_for_server 3600; then
            log "FATAL: Failed to reach healthy Kaggle server after retry. Aborting."
            exit 1
        fi
    fi
fi

# Step 2: Start Periodic Checkpointing (every 30 mins) in the background
EVAL_DIR="artifacts/kaggle_12h_eval_1000"
PRED_FILE="${EVAL_DIR}/predictions.jsonl"
SCORE_FILE="${EVAL_DIR}/score_report.json"
CHECKPOINT_SCORE="${RESULTS_DIR}/checkpoint_score.json"
CHECKPOINT_PREDS="${RESULTS_DIR}/checkpoint_predictions.jsonl"
QUESTIONS_FILE="data/train/questions.jsonl"
REFS_FILE="data/train/reference_answers.jsonl"

mkdir -p "${EVAL_DIR}"

checkpoint_worker() {
    while true; do
        sleep 1800 # 30 minutes
        if [ -f "${PRED_FILE}" ] && [ -s "${PRED_FILE}" ]; then
            local count
            count=$(wc -l < "${PRED_FILE}" | tr -d ' ')
            log "[CHECKPOINT] Scoring current progress (${count} predictions written)..."
            cp "${PRED_FILE}" "${CHECKPOINT_PREDS}" 2>/dev/null || true
            python -m bizreason.cli score \
                --predictions "${PRED_FILE}" \
                --references "${REFS_FILE}" \
                --questions "${QUESTIONS_FILE}" \
                --output "${CHECKPOINT_SCORE}" \
                --subset-predicted 2>/dev/null || true
            if [ -f "${CHECKPOINT_SCORE}" ]; then
                log "[CHECKPOINT] Updated ${CHECKPOINT_SCORE} (${count} questions evaluated so far)."
            fi
        fi
    done
}

# Launch checkpoint worker in background and register cleanup
checkpoint_worker &
CHECKPOINT_PID=$!
log "Started 30-minute checkpoint worker (PID: ${CHECKPOINT_PID})."

cleanup() {
    log "Cleaning up background tasks..."
    if kill -0 "${CHECKPOINT_PID}" 2>/dev/null; then
        kill "${CHECKPOINT_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Step 3: Run the 1000-question Evaluation with up to 3 retries
MAX_RETRIES=3
RETRY_COUNT=0
EVAL_SUCCESS=0

while [ "${RETRY_COUNT}" -le "${MAX_RETRIES}" ]; do
    if [ "${RETRY_COUNT}" -gt 0 ]; then
        log "----------------------------------------------------------------"
        log "Starting evaluation attempt #${RETRY_COUNT} of ${MAX_RETRIES}..."
        log "----------------------------------------------------------------"
    else
        log "----------------------------------------------------------------"
        log "Starting full 1,000-question evaluation (Workers: 4)..."
        log "----------------------------------------------------------------"
    fi

    # Run client eval
    python scripts/kaggle_12h_client.py --eval-limit 1000 --workers 4
    EVAL_RC=$?

    if [ "${EVAL_RC}" -eq 0 ]; then
        log "Evaluation run completed successfully with return code 0!"
        EVAL_SUCCESS=1
        break
    fi

    log "WARNING: Evaluation exited with code ${EVAL_RC}."
    RETRY_COUNT=$((RETRY_COUNT + 1))

    if [ "${RETRY_COUNT}" -gt "${MAX_RETRIES}" ]; then
        log "ERROR: Maximum retries (${MAX_RETRIES}) reached. Evaluation failed."
        break
    fi

    # Resume Safety: Back up existing partial predictions before any rerun
    if [ -f "${PRED_FILE}" ] && [ -s "${PRED_FILE}" ]; then
        PARTIAL_BACKUP="${EVAL_DIR}/predictions_partial_retry${RETRY_COUNT}_$(date +%Y%m%d_%H%M%S).jsonl"
        log "Backing up partial predictions to ${PARTIAL_BACKUP}..."
        cp "${PRED_FILE}" "${PARTIAL_BACKUP}"
        cp "${PARTIAL_BACKUP}" "${RESULTS_DIR}/"
    fi

    log "Checking Kaggle server status before retry..."
    CURRENT_STATUS=$(get_kernel_status)
    log "Kernel status is: ${CURRENT_STATUS}"

    if [[ "${CURRENT_STATUS}" =~ [Cc][Oo][Mm][Pp][Ll][Ee][Tt][Ee] ]] || \
       [[ "${CURRENT_STATUS}" =~ [Ee][Rr][Rr][Oo][Rr] ]] || \
       [[ "${CURRENT_STATUS}" =~ [Ff][Aa][Ii][Ll] ]]; then
        log "Kernel is in finished/error state. Relaunching via push..."
        push_kernel
    fi

    log "Waiting for server to become healthy before retrying eval..."
    if ! wait_for_server 3600; then
        log "Wait timed out or failed. Pushing kernel again..."
        push_kernel
        wait_for_server 3600 || log "Warning: second wait attempt also failed."
    fi

    log "Pausing 15s before starting retry..."
    sleep 15
done

# Step 4: Final Collection and Reporting
log "========================================================================"
log "FINALIZING RESULTS AND REPORTS"
log "========================================================================"

# Copy artifacts to results directory
if [ -f "${SCORE_FILE}" ]; then
    cp -v "${SCORE_FILE}" "${RESULTS_DIR}/score_report.json"
fi

if [ -f "artifacts/run/runtime_report.json" ]; then
    cp -v "artifacts/run/runtime_report.json" "${RESULTS_DIR}/runtime_report.json"
fi

if [ -f "${PRED_FILE}" ]; then
    cp -v "${PRED_FILE}" "${RESULTS_DIR}/predictions.jsonl"
fi

log "------------------------------------------------------------------------"
if [ "${EVAL_SUCCESS}" -eq 1 ] && [ -f "${SCORE_FILE}" ]; then
    log "OVERNIGHT RUN SUMMARY: COMPLETED SUCCESSFULLY"
    log "Results saved to: ${RESULTS_DIR}"
    python -c "
import json
from pathlib import Path
p = Path('${RESULTS_DIR}/score_report.json')
if p.exists():
    try:
        d = json.loads(p.read_text())
        print('Score Report:', json.dumps(d.get('overall', d), indent=2))
    except Exception as e:
        print('Raw report:', p.read_text()[:500])
"
else
    log "OVERNIGHT RUN SUMMARY: FINISHED (Success=${EVAL_SUCCESS})"
    log "Check logs at ${LOG_FILE} and partials in ${RESULTS_DIR}"
fi
log "========================================================================"
