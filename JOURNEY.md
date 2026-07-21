# Project Journey & Problem Solving Log

This document serves as a chronological record of the engineering challenges faced during the development of this GitOps-driven PSN Exporter, and exactly how we solved them.

## 1. The Core Objective
The goal was to build a highly available, GitOps-managed Python exporter that connects to the PlayStation Network (PSNAWP), extracts user trophy and playtime metrics, and pushes them to Grafana Cloud. 
**The primary constraint:** The entire infrastructure must run on an Oracle Cloud "Always Free" Tier VM (1 OCPU, 1GB RAM) without incurring any costs.

## 2. Infrastructure & Tooling Selection
- **Compute:** Oracle Cloud `VM.Standard.E2.1.Micro`.
- **Kubernetes:** We rejected standard K8s and Minikube as they are too heavy for 1GB RAM. We selected **K3s**, specifically stripping out components like Traefik, ServiceLB, and Metrics Server to save ~400MB of RAM.
- **Deployment Strategy:** We rejected Watchtower (too simple) and standard `kubectl apply` (not scalable). We initially selected ArgoCD, but eventually pivoted to **FluxCD** to achieve true GitOps automation without the massive CPU overhead.
- **Data Storage:** Running Prometheus *inside* the 1GB cluster would immediately cause Out-Of-Memory (OOM) crashes. We solved this by using the `prometheus-remote-writer` Python library to push metrics **directly** to Grafana Cloud's remote endpoint, bypassing local storage entirely.

## 3. The Great 1GB RAM Bottleneck
When we initially attempted to deploy ArgoCD alongside K3s on the 1GB VM, the server catastrophically crashed. 
- **The Problem:** The Kubernetes API Server (`etcd`/`sqlite`) and ArgoCD's application controller require massive spikes of memory during initialization. The VM completely exhausted its physical RAM and locked up.
- **The Solution:** We ssh'd into the VM and forcefully detached 15GB of free disk space from the boot volume to create a massive `/swapfile`. This allowed the Linux kernel to page idle memory to the SSD, completely eliminating the OOM crashes. At its peak, the cluster actively utilized over 700MB of Swap memory just to keep K3s and ArgoCD stable!

## 4. Overcoming Server-Side Apply Limitations
When deploying ArgoCD using the standard `install.yaml`, Kubernetes threw a massive error: `Request entity too large: limit is 3145728`.
- **The Problem:** The ArgoCD Custom Resource Definitions (CRDs) were so large that they exceeded the maximum allowed size for a standard client-side `kubectl apply` annotation.
- **The Solution:** We modified the deployment script to use `kubectl apply --server-side`. This forces the Kubernetes API server to handle the merge logic internally, bypassing the annotation size limit entirely.

## 5. Security & The Multi-Cloud CSI Architecture
Instead of storing sensitive PSN and Grafana API tokens directly inside the GitHub repository or as raw Kubernetes Secrets, we engineered a dual-provider Secrets Store CSI setup:
1. **Google Cloud Secret Manager:** We created a dedicated GCP Service Account and used the `provider-gcp-plugin` to dynamically pull the PSN token.
2. **HashiCorp Vault:** We integrated a HashiCorp Vault instance (using Kubernetes Authentication) to store and provide the Grafana API Key.

**The Ultimate Bottleneck:** While the CSI driver was a massive security upgrade, attempting to install the HashiCorp Vault Helm chart *alongside* ArgoCD on a 1 OCPU machine caused the Kubernetes API server to repeatedly timeout and crash (`TLS handshake timeout`). 
**The Compromise:** We realized that while 15GB of Swap solved the *memory* problem, the 1 OCPU was simply too weak to handle the CPU-intensive cryptography and startup routines of an enterprise Vault server. We pivoted to a Hybrid approach: using the CSI Driver exclusively for Google Cloud (which has minimal CPU overhead), and falling back to a native Kubernetes secret for the secondary token.

## 6. Continuous Integration (CI/CD)
To finalize the GitOps loop, we built a GitHub Actions pipeline (`.github/workflows/ci.yml`). 
Whenever new Python code is pushed:
1. GitHub builds a multi-architecture Docker image.
2. It pushes the image to GitHub Container Registry (`ghcr.io`).
3. It automatically rewrites the `kustomization.yaml` file with the new Git SHA tag and commits it back to the repo.
4. FluxCD detects the new commit and instantly rolls out the update to the Oracle VM.

## 7. Automated Expiration Alerting
The PSN NPSSO token expires approximately every 60 days. Instead of relying on external scripts to track the expiration date, we updated the Python exporter to catch the `PSNAWPAuthenticationError` exception. When caught, it pushes a `psn_token_expired=1` metric to Grafana Cloud, triggering an immediate email alert so the token can be rotated via Google Cloud Secret Manager.

## 8. AI/MCP Integration & The Grafana Token Discovery
To take the project a step further, we integrated **Model Context Protocol (MCP)** to allow the Antigravity AI to natively interact with the Grafana Cloud dashboards. 
- **The Problem:** The initial configuration failed with a `401 Unauthorized` API error. 
- **The Discovery:** We realized that Grafana Cloud has two entirely distinct token ecosystems. The token we initially generated (`glc_...` Cloud Access Policy) was strictly a "Prometheus Metrics Publisher" token. This is excellent for backend security (as it limits the exporter pod to only pushing data), but it fundamentally lacks permissions to query the Grafana UI or read dashboard configurations.
- **The Solution:** We explicitly generated a native **Grafana Service Account Token** (`glsa_...`) within the UI, mapped it to the `Viewer` role, and injected it into the local `mcp.json` config. This instantly unlocked the AI's ability to natively fetch and read the `PSN Network Dashboard` and other Observability metrics directly from the chat interface.

## 9. The 1 OCPU Absolute Limit & ArgoCD Overhead
While Swap memory prevented OOM crashes, we continuously hit a hard CPU bottleneck.
- **The Problem:** The `VM.Standard.E2.1.Micro` instance is strictly locked to 1 OCPU. ArgoCD relies heavily on multiple Go-based microservices (Repo Server, Application Controller) to process git hooks and render manifests. When ArgoCD triggered a sync, CPU usage spiked to 100%, completely starving the K3s API Server. This resulted in continuous `TLS handshake timeout` errors and caused `kubectl` commands to fail entirely until K3s was forcefully restarted via `systemctl`.
- **The Solution (Bypass):** We bypassed the ArgoCD OpenAPI validation timeout by manually applying the Kustomize manifests directly to the cluster with `--validate=false` to ensure the PSN pods could start.
- **The Ultimate Resolution (Hardware Upgrade):** We realized this architecture requires more than 1 OCPU. We attempted to provision Oracle's "Always Free" **Arm Ampere A1 Compute** instance (which provides 4 OCPUs and 24GB RAM). However, Oracle was completely out of physical ARM host capacity in the region. We engineered an automated Terraform auto-sniper loop script to continuously attempt provisioning the 24GB machine in the background until Oracle frees up a slot.

## 10. The Architectural Pivot: Migrating to FluxCD
Because the 24GB ARM instance was out of capacity, we were forced to stabilize the existing 1 OCPU VM. 
- **The Problem:** The CPU spikes caused by ArgoCD's heavy Go microservices (specifically the repo-server and redis caches) made the cluster fundamentally unstable.
- **The Solution:** We completely uninstalled ArgoCD from the cluster and replaced it with **FluxCD**. FluxCD executes the exact same GitOps reconciliation loop but does so without a Web UI, Redis caches, or heavy controller meshes. This reduced the idle CPU usage dramatically, finally allowing the 1 OCPU Kubernetes API server to breathe and eliminating the `TLS handshake timeout` errors permanently.

## 11. Debugging the GitHub Actions Pipeline
While verifying the continuous integration pipeline (`ci.yml`), we discovered that the automated Docker build step was silently failing.
- **The Problem:** The pipeline was programmed to tag images dynamically using the `${{ github.repository }}` environment variable. Because the GitHub repository is named `sai7teja/PSN` (with capital letters), the pipeline attempted to push the image as `ghcr.io/sai7teja/PSN`. Docker Container Registries have a strict protocol that repository tags must be exclusively lowercase. Docker rejected the push with an `invalid reference format` error.
- **The Solution:** We refactored the CI workflow file to hardcode the lowercase equivalent (`ghcr.io/sai7teja/psn-exporter`). This ensured the reference format was valid and allowed the GitHub Action to successfully compile and push the new Python exporter image to the registry.
