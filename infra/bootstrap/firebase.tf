# Firebase on the project (D4). Firebase Hosting serves the dashboard (module phr), and a Hosting
# site can only exist in a Firebase project. Adding Firebase needs firebase.projects.update, which
# among predefined roles only owner, editor, roles/firebase.admin and roles/firebase.editor hold.
# Bootstrap already runs once with the owner's credentials, so it is done here and the deployer
# keeps only roles/firebasehosting.admin.
#
# One-way: "Once Firebase has been added to a Google Project it cannot be removed" (provider
# docs, google_firebase_project). Destroying this resource does not remove Firebase. It costs
# nothing; Hosting bills only storage above 10 GB and transfer above 10 GB a month.

resource "google_firebase_project" "this" {
  count    = var.add_firebase ? 1 : 0
  provider = google-beta

  project = local.project_id

  # The Firebase Management API must be on first (project.tf, bootstrap_apis).
  depends_on = [google_project_service.bootstrap]
}
