# Detailed GitOps Architecture & Deployment Guide

This document provides a minute, step-by-step technical breakdown of how this infrastructure is built, secured, and deployed.

## 1. Infrastructure (Oracle Cloud Free Tier)
To run a full Kubernetes cluster (K3s), ArgoCD, and a HashiCorp Vault server on a machine with only 1GB of RAM, severe optimizations are required.

### Memory Optimization (15GB Swap)
Because the `VM.Standard.E2.1.Micro` instance comes with a 45GB boot volume but only 1GB of physical RAM, we repurpose 15GB of disk space into virtual memory:
```bash
sudo fallocate -l 15G /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```
Without this swap file, K3s and ArgoCD will immediately crash with `Out of Memory (OOM)` errors.

### K3s Installation
We install a stripped-down version of K3s. By disabling `traefik`, `servicelb`, and `metrics-server`, we save approximately 400MB of RAM.
```bash
curl -sfL https://get.k3s.io | sh -s - server --disable traefik --disable servicelb --disable metrics-server
```

## 2. Multi-Cloud Secrets Architecture
For enterprise-grade security, **no API keys are stored in Kubernetes Secrets natively**. Instead, we use the **Kubernetes Secrets Store CSI Driver** to dynamically pull secrets from two completely separate cloud providers at runtime.

### Provider 1: Google Cloud Secret Manager (PSN Token)
- **Identity:** We created a dedicated Google Cloud Service Account (`psn-k8s-sa`) with the `SecretManager SecretAccessor` IAM role.
- **Authentication:** The Service Account JSON key is injected into the cluster as a bootstrap secret (`gcp-sa-secret`).
- **CSI Plugin:** The `provider-gcp-plugin` intercepts pod creation and uses the JSON key to authenticate with Google Cloud.
- **Manifest:** `k8s/base/secret-provider-gcp.yaml` tells the CSI driver to fetch the exact secret `projects/.../secrets/psn-token/versions/latest` and mount it as a file named `PSN_TOKEN`.

### Provider 2: HashiCorp Vault (Grafana API Key)
- **Deployment:** A lightweight HashiCorp Vault server runs in `dev` mode directly inside the K3s cluster.
- **Authentication:** We enabled Vault's Kubernetes Authentication method. The Vault server trusts the K3s API server.
- **RBAC:** We created a Vault Role (`psn-role`) bound to the `psn-exporter` ServiceAccount. When the exporter pod spins up, Vault verifies the pod's identity via the Kubernetes TokenReview API.
- **Manifest:** `k8s/base/secret-provider-vault.yaml` requests the `grafana-token` secret from Vault.

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
When the pod starts, the CSI driver connects to both Google Cloud and Vault, downloads the secrets, mounts them to the filesystem (`/mnt/gcp-secrets`), and simultaneously syncs them into standard Kubernetes environment variables so the Python script can read them!

## 3. CI/CD Pipeline (GitHub Actions)
The deployment is 100% automated via GitOps.

1. **Build:** When code is pushed to the `main` branch, `.github/workflows/ci.yml` triggers.
2. **Push:** It uses Docker Buildx to compile the Python script into a container image and pushes it to the GitHub Container Registry (`ghcr.io`).
3. **Kustomize Update:** The pipeline uses `kustomize edit set image` to update the `k8s/base/kustomization.yaml` file with the exact Git SHA of the new image, and commits the change back to the repository.
4. **ArgoCD Sync:** ArgoCD (running on the Oracle VM) detects the commit, pulls the new `kustomization.yaml`, and gracefully rolls out the new container to the cluster!

## 4. Alerting
If the PSN Token expires (every ~60 days), the `psnawp` library will throw a 401 Authentication Error.
The Python exporter catches this error and pushes a custom metric to Grafana: `psn_token_expired = 1`.
A Grafana Alert rule monitors this metric and will immediately dispatch an email/Discord notification so the token can be rotated via Google Cloud Secret Manager.
