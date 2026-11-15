/// <reference types="vite/client" />

// Build-time settings, read from VITE_* environment variables (see .env.example and README).
// Vite exposes only variables with the VITE_ prefix to the page, and inlines them into the
// bundle, so nothing secret may ever be put in one.
interface ImportMetaEnv {
  /** Base URL of fhir-proxy. Empty or unset means "/api" on the page's own origin. */
  readonly VITE_FHIR_PROXY_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
