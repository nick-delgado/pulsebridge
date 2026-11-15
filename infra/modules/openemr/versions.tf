# This module creates no resources and declares no provider: it only renders boot hook scripts and
# connection settings from its inputs (README, "What this module is"). With no provider, nothing in
# it can call a Google API, during plan or test, with or without credentials.
terraform {
  # Same floor as the other modules.
  required_version = ">= 1.11"
}
