# Immune API

![CI tests](https://github.com/41R3/immune-api/actions/workflows/ci.yml/badge.svg)

**An artificial-immune-system-inspired intrusion detection system for serverless APIs.**

Immune API is a lightweight security middleware for AWS Lambda that learns normal API behavior from **request metadata only**. It uses a small autoencoder to detect anomalous traffic patterns and temporarily block abusive sources.

---

## Key Highlights

- **Privacy by Design:** Analyzes request metadata (timing, rates, duration, status) without inspecting request payloads (`event["body"]`).
- **Low Overhead:** Pure Python inference code running directly in AWS Lambda without heavy ML frameworks.
- **Adaptive Detection:** Learns normal traffic behavior dynamically instead of relying purely on rigid rate limits.
- **Robust Testing & CI:** Fully tested locally (**86 tests, ~91% coverage**) with automated quality gates.

> **Project Status:** Research prototype. Local simulation, training pipeline, test suite, and Terraform infrastructure are implemented and statically validated, but **not yet deployed to a real production AWS account**.

---

## How It Works

A request passes through a guarded Lambda handler:
1. **Pre-check:** Rejects immediately if the source IP/identifier is currently blocked (429).
2. **Handler Execution:** Your normal serverless business logic runs.
3. **Post-evaluation:** 
   - Records request metadata into a local rolling window.
   - Builds a 12-dimensional feature vector (rate, regularity, compute cost, errors, etc.).
   - Scores the vector via a lightweight tanh autoencoder.
   - Confirms sustained anomalies over time and applies temporary blocks via DynamoDB using a `strikes` mechanism.

---

## Project Structure

| Path | Purpose |
|---|---|
| `immune/features.py` | 12-dimensional feature vector extraction |
| `immune/model.py` | Lightweight inference autoencoder & normalization |
| `immune/train.py` | NumPy training pipeline on synthetic traffic |
| `immune/guard.py` | Middleware, rolling state, confirmation logic & strikes |
| `immune/simulate.py` | Traffic replay and security impact simulation |
| `lambda/handler.py` | Example AWS Lambda handler using `@GUARD.protect` |
| `terraform/` | AWS infrastructure (Lambda, HTTP API, DynamoDB, SNS) |
| `tests/` | Comprehensive test suite (Unit, Lambda, Terraform) |

---

## Quick Start

### Requirements
- Python **3.10+**
- `zip` for Lambda packaging

### 1. Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

### 2. Run Tests
```bash
pytest -v
pytest --cov=immune --cov-report=term-missing
```

### 3. Train & Evaluate the Model
```bash
scripts/train_model.sh
scripts/evaluate_model.sh model/model.json
python3 scripts/check_thresholds.py build/eval_summary.json
```

### 4. Build Lambda Package
```bash
scripts/build_lambda.sh model/model.json
```
Outputs `build/app.json` (~12 KB without NumPy).

---

## Evaluation Results (Synthetic Data)

Evaluated against 297 normal sources and synthetic attack profiles:

| Traffic Type | Blocked | Median Time to Block | Cost Avoided |
|---|---:|---:|---:|
| Scanner | 100% | 10.3 s | 79.7% |
| Credential stuffing | 100% | 15.7 s | 78.1% |
| Denial-of-Wallet flood | 95% | 5.3 s | 90.4% |
| Oversized-event injection | 85% | 33.2 s | 52.9% |
| Expensive low-and-slow | 60% | 31.9 s | 46.9% |
| **Normal traffic** | **0%** | — | — |

---
<img width="1093" height="631" alt="Captura de pantalla 2026-09-30 160617" src="https://github.com/user-attachments/assets/3034dd7a-80da-4d20-8948-6421b24360d1" />

## AWS & Terraform

Deployable infrastructure configurations are included under `terraform/`.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform apply
```
*Note: Keep `guard_mode = "monitor"` during initial deployment to observe detection logs before enforcing blocks (`enforce`).*

---

## License

MIT License (or specify your license here).
