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

## 2. Multi-Cloud Secrets Architecture
For enterprise-grade security, we use a hybrid approach. We use the **Kubernetes Secrets Store CSI Driver** to dynamically pull the primary PSN authentication token from Google Cloud at runtime, and rely on standard Kubernetes Secrets for secondary configuration tokens to save K3s API CPU overhead.

### Provider 1: Google Cloud Secret Manager (PSN Token)
- **Identity:** We created a dedicated Google Cloud Service Account (`psn-k8s-sa`) with the `SecretManager SecretAccessor` IAM role.
- **Authentication:** The Service Account JSON key is injected into the cluster as a bootstrap secret (`gcp-sa-secret`).
- **CSI Plugin:** The `provider-gcp-plugin` intercepts pod creation and uses the JSON key to authenticate with Google Cloud.
- **Manifest:** `k8s/base/secret-provider-gcp.yaml` tells the CSI driver to fetch the exact secret `projects/.../secrets/psn-token/versions/latest` and mount it as a file named `PSN_TOKEN`.

### Provider 2: Native Kubernetes Secrets (Grafana API Key)
- **Deployment:** Standard Kubernetes Opaque Secrets.
- **Reasoning:** We originally attempted to run HashiCorp Vault locally in the cluster. However, the cryptographic startup routines and sidecar injections of Vault required far too much CPU processing for the 1 OCPU machine to handle alongside the GitOps controller. We pivoted to native secrets for the secondary token to ensure stability.

### Pod Integration
In `deployment.yaml`, the pod specifies two CSI volumes:
```yaml
      volumes:
        - name: gcp-secrets-store
          csi:
            driver: secrets-store.csi.k8s.io
            volumeAttributes:
              secretProviderClass: "gcp-provider"
```
When the pod starts, the CSI driver connects to Google Cloud, downloads the primary secret, mounts it to the filesystem (`/mnt/gcp-secrets`), and simultaneously syncs it into standard Kubernetes environment variables so the Python script can read it alongside the Grafana token!

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
