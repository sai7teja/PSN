variable "compartment_ocid" {
  description = "The OCID of the compartment where resources will be created. (Often the same as your Tenancy OCID)"
  type        = string
}

variable "ssh_public_key_path" {
  description = "Path to your SSH public key. This will be injected into the VM so you can log in."
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}
