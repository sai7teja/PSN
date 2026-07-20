#!/bin/bash
set -e

echo "========================================="
echo " Bootstrapping Oracle Cloud 1GB Micro VM "
echo "========================================="

echo "1. Creating 15GB Swap File (Crucial for 1GB RAM limits)..."
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

echo "2. Installing Lightweight K3s..."
# We disable Traefik, ServiceLB, and the Metrics Server to save ~300MB of RAM.
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik --disable servicelb --disable metrics-server" sh -

echo "Waiting for K3s to initialize (20 seconds)..."
sleep 20

echo "3. Installing FluxCD (Lightweight GitOps Controller)..."
sudo k3s kubectl apply -f https://github.com/fluxcd/flux2/releases/latest/download/install.yaml --server-side
echo "Waiting for FluxCD to initialize (15 seconds)..."
sleep 15
echo "Applying Flux configuration..."
sudo k3s kubectl apply -f https://raw.githubusercontent.com/sai7teja/PSN/main/flux/psn-source.yaml
sudo k3s kubectl apply -f https://raw.githubusercontent.com/sai7teja/PSN/main/flux/psn-kustomization.yaml

echo "FluxCD GitOps Sync Initialized!"
echo "========================================="
echo "✅ VM Successfully Bootstrapped!"
echo "========================================="
