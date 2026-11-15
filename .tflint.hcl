# TFLint configuration for every Terraform directory under infra/ (T26). Run it with `make tf-lint`,
# which installs the pinned tflint and Google ruleset and passes this file to each directory.
#
# TFLint is a linter: it reads the configuration (no credentials, no API calls) and reports
# mistakes `terraform validate` accepts, such as an invalid machine type or region, an unused
# declaration or a missing version constraint.

config {
  # Follow local module calls (../../modules/...) so a module's inputs are checked where they are
  # used. Needs no `terraform init`; this repository uses no remote modules.
  call_module_type = "local"
}

# The bundled Terraform language rules, "recommended" preset: required versions and providers,
# no unused declarations, typed variables, deprecated syntax.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

# Google Cloud rules: invalid machine types, regions, disk types and similar values that would
# only fail at apply time. deep_check (which calls Google APIs) stays off: CI lints without
# credentials. Keep `version` equal to TFLINT_GOOGLE_VERSION in the Makefile.
plugin "google" {
  enabled = true
  version = "0.41.0"
  source  = "github.com/terraform-linters/tflint-ruleset-google"
}
