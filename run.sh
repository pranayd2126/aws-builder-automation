#!/usr/bin/env bash
# =============================================================
# AWS Builder Center Automation — Cron entry point
# Activates the Python venv and runs the application once.
# =============================================================
set -euo pipefail

PROJECT_DIR="/home/ubuntu/aws-builder-automation"
cd "$PROJECT_DIR"

# Ensure runtime directories exist
mkdir -p data logs

# Activate virtual environment
source .venv/bin/activate

# Run the application (single execution, then exit)
python -m app.main

deactivate
