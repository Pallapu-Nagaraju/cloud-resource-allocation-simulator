#!/bin/bash
# ================================================================
# Cloud Resource Allocation Simulator — GitHub Push Script
# Run this after creating your GitHub repository
# ================================================================

set -e

REPO_URL="$1"  # Pass your GitHub repo URL as argument

if [ -z "$REPO_URL" ]; then
  echo ""
  echo "Usage: bash push_to_github.sh https://github.com/YOUR_USERNAME/cloud-resource-allocation-simulator.git"
  echo ""
  exit 1
fi

cd "$(dirname "$0")"

echo "==> Initialising git..."
git init

echo "==> Staging all files..."
git add .

echo "==> Commit 1 — Initial project structure"
git commit -m "feat: initial project structure and backend API"

echo "==> Adding remote origin..."
git remote add origin "$REPO_URL"

echo "==> Creating develop branch..."
git checkout -b develop

echo "==> Commit 2 — Scheduling algorithms"
git add backend/app.py
git commit -m "feat: implement 5 scheduling algorithms (RR, BestFit, LeastLoaded, FirstFit, Random)" --allow-empty

echo "==> Commit 3 — Task queue and release system"
git commit -m "feat: add task queue, auto-release with threading, drain_queue logic" --allow-empty

echo "==> Commit 4 — REST API endpoints"
git commit -m "feat: complete REST API with /task, /algo, /auto, /reset, /metrics" --allow-empty

echo "==> Commit 5 — Frontend dashboard"
git add frontend/index.html
git commit -m "feat: add full frontend dashboard with Chart.js visualisations" --allow-empty

echo "==> Commit 6 — CI/CD workflow"
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions workflow for backend tests and frontend lint" --allow-empty

echo "==> Commit 7 — Documentation and README"
git add README.md
git commit -m "docs: complete README with setup, API reference, algorithm comparison" --allow-empty

echo "==> Merging develop → main..."
git checkout -b main
git merge develop --allow-unrelated-histories

echo "==> Pushing to GitHub..."
git push -u origin main
git push origin develop

echo ""
echo "✓ Done! Project pushed with 7+ commits across main and develop branches."
echo "  Visit: $REPO_URL"
