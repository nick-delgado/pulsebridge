/**
 * Picks the client for this build. The mock and HTTP clients implement the same interface, so
 * nothing else in the page knows which one it has (except to show the mock-mode notice).
 *
 * The mode is a build-time constant, so in an HTTP build the `import('./mock-client')` branch
 * is removed and the mock code and data are not in the bundle (scripts/check-bundle.mjs checks).
 */
import { proxyBaseUrl } from '../config';
import type { PulseBridgeApi } from './contract';
import { HttpClient } from './http-client';
import type { Session } from './session';

export interface ClientDeps {
  session: Session;
  onUnauthorized: () => void;
}

export async function createClient(deps: ClientDeps): Promise<PulseBridgeApi> {
  // Compare import.meta.env.MODE here, not the API_MODE constant: Vite replaces it with a literal
  // in this module, so the bundler sees `if ("production" === "mock")` and drops the import.
  if (import.meta.env.MODE === 'mock') {
    const { MockClient } = await import('./mock-client');
    return new MockClient(deps);
  }
  return new HttpClient({ baseUrl: proxyBaseUrl(), ...deps });
}

export type * from './contract';
export { ApiError, LoginError, UnauthorizedError } from './errors';
export { Session } from './session';
