# No provider configuration and no backend here: a module inherits its providers from the root
# module that calls it (infra/envs/*), so the same module works against any project.
terraform {
  # 1.11 is the repo-wide floor (write-only arguments, cross-variable validation).
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
    # Firebase Hosting (google_firebase_hosting_site) and google_firebase_project exist only in
    # the beta provider at 8.11: `terraform providers schema` for hashicorp/google 8.11.0 lists no
    # google_firebase_hosting_* resource. The env root must configure `google-beta` with the
    # same project and region as `google` (README, "For T25").
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.11"
    }
  }
}
