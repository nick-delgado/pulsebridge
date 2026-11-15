import type { OperationOutcome } from './fhir';

/**
 * A protected call was refused with 401 (token missing, expired, altered, or signed under an old
 * password). By the time this is thrown the session is already cleared and the app has been told
 * to show the login form, so callers only need to stop what they were doing.
 */
export class UnauthorizedError extends Error {
  override readonly name = 'UnauthorizedError';
  constructor() {
    super('Your session has ended. Log in again.');
  }
}

export type LoginFailure = 'wrong-password' | 'rate-limited' | 'not-configured' | 'unavailable';

/** Login failed. `reason` maps the proxy's status: 401, 429, 503, anything else. */
export class LoginError extends Error {
  override readonly name = 'LoginError';
  constructor(
    readonly reason: LoginFailure,
    message: string,
    /** Seconds from the 429 `Retry-After` header, when given. */
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
  }
}

/** Any other non-2xx answer, or a network failure (status 0). */
export class ApiError extends Error {
  override readonly name = 'ApiError';
  constructor(
    readonly status: number,
    message: string,
    /** The FHIR OperationOutcome from the body, when the proxy sent one. */
    readonly outcome: OperationOutcome | null = null,
    /** Seconds from a 429 `Retry-After` header (the rate-limited cuff route), when given. */
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
  }
}
