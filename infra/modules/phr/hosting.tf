# Firebase Hosting (D4): serves the built dashboard (web/dist) and puts fhir-proxy under /api on
# the same origin with a rewrite.
#
# What Terraform can manage, and what it cannot:
#   - Terraform creates the Hosting SITE (and, if asked, adds Firebase to the project).
#   - Terraform does NOT upload the page. google_firebase_hosting_version at google-beta 8.11 is
#     "a configuration which determine how a site is displayed. Static files are not supported
#     at the moment." (provider docs, firebase_hosting_version). So no version or release is
#     created here: a Terraform release would carry no files, and re-creating it on a config
#     change would replace the live page with an empty site.
#   - The site's configuration (the /api/** rewrite, the security headers, what is not deployed)
#     travels with each deploy in firebase.json. This module RENDERS that file (output
#     firebase_json) from its own values, so the service name, region, site and CSP are never
#     written by hand; the deploy step (README, "Deploying the page") writes it next to
#     web/dist and runs the Firebase CLI.

locals {
  # The response headers of every page response: committed as JSON so the CSP check
  # (tests/check_hosting_csp.py) reads the same bytes Terraform does. The CSP is the one the
  # page's build puts in its <meta> tag for a same-origin build (web/vite.config.ts), plus
  # frame-ancestors, which only a header can set (web/README.md, "Security basics").
  security_headers = jsondecode(file("${path.module}/hosting/security-headers.json"))

  # firebase.json's "hosting" object (firebase.google.com/docs/hosting/full-config).
  firebase_hosting = {
    site   = local.site_id
    public = "dist" # relative to firebase.json, which the deploy step writes into web/
    # Firebase's default ignore list. "**/.*" keeps dot-directories out, which is what keeps
    # Vite's build manifest (dist/.vite/manifest.json) off the site; it is spelled out once
    # more so that a later edit of the defaults cannot publish it.
    ignore = ["firebase.json", "**/.*", "**/node_modules/**", "**/.vite/**"]
    # "Rewrites to Cloud Run": every /api/... request goes to fhir-proxy with its full path.
    # pinTag stays off: it would add Cloud Run traffic tags that Terraform does not manage (and
    # would remove on the next apply).
    rewrites = [{
      source = "${local.path_prefix}/**"
      run = {
        serviceId = google_cloud_run_v2_service.fhir_proxy.name
        region    = var.region
        pinTag    = false
      }
    }]
    headers = [
      # Security headers on everything Hosting serves. Header patterns match the request URL
      # before rewrites apply, so /api/** responses get them too; for JSON they change nothing
      # (fhir-proxy sends its own stricter "default-src 'none'" policy).
      {
        source  = "**"
        headers = [for k in sort(keys(local.security_headers)) : { key = k, value = local.security_headers[k] }]
      },
      # Vite names assets by content hash (/assets/index-<hash>.js): safe to cache for a year.
      {
        source  = "/assets/**"
        headers = [{ key = "Cache-Control", value = "public, max-age=31536000, immutable" }]
      },
      # The page itself must be revalidated, or a browser could keep an old index.html that
      # names assets a new deploy no longer has.
      {
        source  = "/"
        headers = [{ key = "Cache-Control", value = "no-cache" }]
      },
      {
        source  = "/index.html"
        headers = [{ key = "Cache-Control", value = "no-cache" }]
      },
    ]
  }
}

# Firebase must be added to the Google Cloud project before it can have a Hosting site. It is a
# one-way switch ("Once Firebase has been added to a Google Project it cannot be removed",
# provider docs, firebase_project), so by default it is done once outside this module
# (manage_firebase_project, README "Adding Firebase to the project").
resource "google_firebase_project" "this" {
  count    = var.manage_firebase_project ? 1 : 0
  provider = google-beta

  project = var.project_id
}

# The Hosting site: https://<site_id>.web.app. Free to have; billed only for storage above 10 GB
# and transfer above 10 GB a month.
resource "google_firebase_hosting_site" "dashboard" {
  provider = google-beta

  project = var.project_id
  site_id = local.site_id

  depends_on = [google_firebase_project.this]
}
