terraform {
  required_version = ">= 1.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 5.0"
    }
  }
}

# Uses the local OCI CLI config (usually ~/.oci/config) for authentication
provider "oci" {
  config_file_profile = "DEFAULT"
}

# Fetch the Availability Domains in your compartment
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_ocid
}

# ============================================================================
# NETWORKING
# ============================================================================

resource "oci_core_vcn" "psn_vcn" {
  compartment_id = var.compartment_ocid
  display_name   = "psn-gitops-vcn"
  cidr_block     = "10.0.0.0/16"
}

resource "oci_core_internet_gateway" "psn_igw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.psn_vcn.id
  display_name   = "psn-igw"
}

resource "oci_core_default_route_table" "psn_rt" {
  manage_default_resource_id = oci_core_vcn.psn_vcn.default_route_table_id

  route_rules {
    network_entity_id = oci_core_internet_gateway.psn_igw.id
    destination       = "0.0.0.0/0"
  }
}

# Firewall Rules
resource "oci_core_default_security_list" "psn_sl" {
  manage_default_resource_id = oci_core_vcn.psn_vcn.default_security_list_id

  # Allow all outbound traffic
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  # Allow inbound SSH (22)
  ingress_security_rules {
    protocol = "6" # TCP
    source   = "0.0.0.0/0"
    tcp_options {
      min = 22
      max = 22
    }
  }

  # Allow inbound HTTP (80) for Traefik Ingress / Let's Encrypt
  ingress_security_rules {
    protocol = "6" # TCP
    source   = "0.0.0.0/0"
    tcp_options {
      min = 80
      max = 80
    }
  }

  # Allow inbound HTTPS (443) for Grafana and ArgoCD UI
  ingress_security_rules {
    protocol = "6" # TCP
    source   = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_subnet" "psn_subnet" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.psn_vcn.id
  cidr_block     = "10.0.1.0/24"
  display_name   = "psn-subnet"
}

# ============================================================================
# COMPUTE INSTANCE (VM.Standard.E2.1.Micro)
# ============================================================================

# Dynamically fetch the latest Ubuntu 22.04 image for AMD64 (E2.1.Micro shape)
data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04"
  shape                    = "VM.Standard.E2.1.Micro"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_instance" "psn_vm" {
  # Deploy to the first Availability Domain
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  compartment_id      = var.compartment_ocid
  display_name        = "psn-gitops-vm"
  shape               = "VM.Standard.E2.1.Micro"

  create_vnic_details {
    subnet_id        = oci_core_subnet.psn_subnet.id
    display_name     = "primaryvnic"
    assign_public_ip = true
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.ubuntu.images[0].id
    # 50GB Boot Volume gives us plenty of room for K3s, ArgoCD, and the 4GB Swap file.
    # Note: Oracle Always Free provides up to 200GB total across all instances.
    boot_volume_size_in_gbs = 50 
  }

  metadata = {
    ssh_authorized_keys = file(var.ssh_public_key_path)
  }
}

output "public_ip" {
  description = "The public IP address of the newly created Oracle VM"
  value       = oci_core_instance.psn_vm.public_ip
}
