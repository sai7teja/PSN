# Detailed GitOps Architecture & Deployment Guide

This document provides a minute, step-by-step technical breakdown of how this infrastructure is built, secured, and deployed.

## 1. Infrastructure (Oracle Cloud Free Tier)
To run a full Kubernetes cluster (K3s) and FluxCD on a machine with only 1GB of RAM, severe optimizations are required.

### Memory Optimization (15GB Swap)
Because the `VM.Standard.E2.1.Micro` instance comes with a 45GB boot volume but only 1GB of physical RAM, we repurpose 15GB of disk space into virtual memory:
```bash
sudo fallocate -l 15G /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```
Without this swap file, K3s and FluxCD will immediately crash with `Out of Memory (OOM)` errors.

### K3s Installation
We install a stripped-down version of K3s. By disabling `traefik`, `servicelb`, and `metrics-server`, we save approximately 400MB of RAM.
```bash
curl -sfL https://get.k3s.io | sh -s - server --disable traefik --disable servicelb --disable metrics-server
```

## 2. Secrets Architecture Strategy

For this deployment, we rely on **Native Kubernetes Secrets manually populated via SSH**. 
Because this cluster runs on an Oracle `VM.Standard.E2.1.Micro` instance (1 OCPU, 1GB RAM), we face severe CPU and memory constraints. 

### Why not fully automated GitOps Secrets? (The Constraints)
If we were running on a powerful VM or managed Kubernetes service (EKS/GKE), we would use one of the following approaches:
1. **Secrets Store CSI Driver (GCP Secret Manager / HashiCorp Vault):** This allows pods to fetch secrets dynamically at runtime from external cloud vaults. 
   - *Limitation:* The CSI Driver runs DaemonSets and sidecars that constantly sync and authenticate. Under 1 OCPU starvation, the crypto and API handshakes time out, throwing pods into `CreateContainerConfigError` loops.
2. **Bitnami SealedSecrets / External Secrets Operator:** This allows you to commit encrypted secrets directly to GitHub. A controller inside the cluster decrypts them and converts them into native Kubernetes Secrets.
   - *Limitation:* The controller demands continuous memory and CPU to watch for changes and run decryption cycles, which frequently causes `Out of Memory (OOM)` errors on 1GB instances alongside FluxCD.

### The Chosen Approach: Native Manual Secrets (Option 1)
To ensure absolute stability, we bypass heavy controllers and keep our credentials off GitHub entirely.
- **Identity & Tokens:** We store the `PSN_TOKEN` and `GRAFANA_TOKEN` locally on the VM.
- **Process:** Whenever a token expires, we SSH into the VM and run a fast `kubectl create secret generic...` command to securely inject the token into the K3s datastore.
- **Pod Integration:** The `psn-exporter` pod natively maps these lightweight K8s Secrets directly into environment variables with zero CPU overhead.

While it lacks the "cool factor" of fully automated GitOps decryption, it is the *only* stable way to manage secrets securely within a 1GB/1-OCPU constraint without constant node crashes.

## 3. CI/CD Pipeline (GitHub Actions)
The deployment is 100% automated via GitOps.

1. **Build:** When code is pushed to the `main` branch, `.github/workflows/ci.yml` triggers.
2. **Push:** It uses Docker Buildx to compile the Python script into a container image and pushes it to the GitHub Container Registry (`ghcr.io`).
3. **Kustomize Update:** The pipeline uses `kustomize edit set image` to update the `k8s/base/kustomization.yaml` file with the exact Git SHA of the new image, and commits the change back to the repository.
4. **FluxCD Sync:** FluxCD (running on the Oracle VM) detects the commit, pulls the new `kustomization.yaml`, and gracefully rolls out the new container to the cluster!

## 4. Alerting
If the PSN Token expires (every ~60 days), the `psnawp` library will throw a 401 Authentication Error.
The Python exporter catches this error and pushes a custom metric to Grafana: `psn_token_expired = 1`.
A Grafana Alert rule monitors this metric and will immediately dispatch an email/Discord notification so the token can be rotated via Google Cloud Secret Manager.
