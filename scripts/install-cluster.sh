#!/bin/bash
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}          PSN GitOps Cluster - Complete Installation Wizard           ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo "This script will completely bootstrap your 1 OCPU Oracle Free Tier VM."
echo "It handles K3s, Swap, Helm, CSI Secrets, Native Secrets, and FluxCD."
echo ""

# Pre-flight check
if [ ! -f "gcp-sa-key.json" ]; then
    echo -e "${RED}ERROR: gcp-sa-key.json not found in the current directory!${NC}"
    echo "Please upload your GCP Service Account JSON key to this folder and name it 'gcp-sa-key.json'."
    echo "Then run this script again."
    exit 1
fi

echo -e "${YELLOW}Please enter your Grafana Cloud credentials:${NC}"
read -p "Grafana Cloud URL (e.g. https://sai7teja.grafana.net/): " GRAFANA_URL
read -p "Grafana Cloud API Token: " GRAFANA_TOKEN

if [ -z "$GRAFANA_URL" ] || [ -z "$GRAFANA_TOKEN" ]; then
    echo -e "${RED}ERROR: Grafana credentials cannot be empty!${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}Starting Installation Flow...${NC}"
echo ""

echo -e "${BLUE}[1/7] Creating 15GB Swap File...${NC}"
if [ ! -f /swapfile ]; then
    sudo fallocate -l 15G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "✅ Swap created successfully."
else
    echo "✅ Swapfile already exists."
fi

echo -e "${BLUE}[2/7] Installing Lightweight K3s...${NC}"
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik --disable servicelb --disable metrics-server" sh -
echo "Waiting for K3s to initialize (20 seconds)..."
sleep 20
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo -e "${BLUE}[3/7] Installing Helm & CSI Secret Drivers...${NC}"
curl -sL https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-3 | bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm repo add secrets-store-csi-driver https://kubernetes-sigs.github.io/secrets-store-csi-driver/charts
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm repo update
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm upgrade --install csi-secrets-store secrets-store-csi-driver/secrets-store-csi-driver --namespace kube-system
sudo k3s kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/secrets-store-csi-driver-provider-gcp/main/deploy/provider-gcp-plugin.yaml

echo -e "${BLUE}[4/7] Creating Kubernetes Namespace...${NC}"
sudo k3s kubectl create namespace psn --dry-run=client -o yaml | sudo k3s kubectl apply -f -

echo -e "${BLUE}[5/7] Provisioning Native Secrets...${NC}"
sudo k3s kubectl create secret generic gcp-sa-secret \
    --from-file=key.json=gcp-sa-key.json \
    -n psn --dry-run=client -o yaml | sudo k3s kubectl apply -f -

sudo k3s kubectl create secret generic grafana-credentials \
    --from-literal=GRAFANA_URL="$GRAFANA_URL" \
    --from-literal=GRAFANA_TOKEN="$GRAFANA_TOKEN" \
    -n psn --dry-run=client -o yaml | sudo k3s kubectl apply -f -
echo "✅ Secrets created successfully."

echo -e "${BLUE}[6/7] Installing FluxCD (Lightweight GitOps)...${NC}"
sudo k3s kubectl apply -f https://github.com/fluxcd/flux2/releases/latest/download/install.yaml --server-side
echo "Waiting for FluxCD to initialize (15 seconds)..."
sleep 15

echo -e "${BLUE}[7/7] Applying GitOps Sync Configuration...${NC}"
sudo k3s kubectl apply -f https://raw.githubusercontent.com/sai7teja/PSN/main/flux/psn-source.yaml
sudo k3s kubectl apply -f https://raw.githubusercontent.com/sai7teja/PSN/main/flux/psn-kustomization.yaml

echo -e "${GREEN}======================================================================${NC}"
echo -e "${GREEN}✅ FULL CLUSTER INSTALLATION COMPLETE!${NC}"
echo -e "${GREEN}======================================================================${NC}"
echo "FluxCD is now syncing your repository."
echo "Check GitOps status:   sudo k3s kubectl get kustomizations -n flux-system"
echo "Check PSN Pods:        sudo k3s kubectl get pods -n psn"
