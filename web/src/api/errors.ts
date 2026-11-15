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
  // Typed `string` so a subclass (CuffSendError) can name itself.
  override readonly name: string = 'ApiError';
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

/**
 * Why a cuff send was refused, from the status the cuff simulator (through the proxy) answered;
 * see services/cuff-sim/README.md, "Responses":
 *
 *   status  reason          when                                                 sent?
 *   400     invalid-body    the body is not a JSON object                        no
 *   413     too-large       the body is over 16 KiB                              no
 *   422     invalid-input   bounds, type, unknown field or sample, a sample      no
 *                           not for this transport (`fields` names them)
 *   429     rate-limited    too many sends; `retryAfterSeconds` from             no
 *                           `Retry-After`
 *   500     server-error    a built message failed HL7v2 validation, or a bug    no (the service
 *                                                                                 cannot 500 once
 *                                                                                 sending began)
 *   504     no-answer       cut off upstream (Cloud Run's request timeout): the  maybe, and not
 *                           attempt MAY have been sent                           recorded
 *
 * Any other status (for example 502 from the proxy) stays a plain `ApiError`.
 */
export type CuffSendFailure =
  'invalid-body' | 'too-large' | 'invalid-input' | 'rate-limited' | 'server-error' | 'no-answer';

export const CUFF_FAILURE_BY_STATUS: Readonly<Partial<Record<number, CuffSendFailure>>> = {
  400: 'invalid-body',
  413: 'too-large',
  422: 'invalid-input',
  429: 'rate-limited',
  500: 'server-error',
  504: 'no-answer',
};

/** A refused cuff send. Still an `ApiError`, so generic handling (`status`, `message`) works. */
export class CuffSendError extends ApiError {
  override readonly name: string = 'CuffSendError';
  constructor(
    readonly reason: CuffSendFailure,
    status: number,
    message: string,
    outcome: OperationOutcome | null = null,
    retryAfterSeconds: number | null = null,
  ) {
    super(status, message, outcome, retryAfterSeconds);
  }

  /**
   * The request fields the OperationOutcome names (each issue's `expression`, for example
   * `systolic`), so a form can mark them. Empty when the body named none.
   */
  get fields(): string[] {
    return [...new Set((this.outcome?.issue ?? []).flatMap((i) => i.expression ?? []))];
  }

  /**
   * The typed error for a cuff-route `ApiError`, or the error unchanged for any other status.
   *
   * cuff-sim's issues are a field plus a FRAGMENT: `expression: ["systolic"]`,
   * `diagnostics: "is required"` (services/cuff-sim/cuff_sim/request.py). The message therefore
   * puts the two together, one issue per clause: "systolic: is required; diastolic: is required".
   * An issue without an expression is its diagnostics alone.
   */
  static from(error: ApiError): ApiError {
    const reason = CUFF_FAILURE_BY_STATUS[error.status];
    if (reason === undefined) return error;
    const message = issueMessage(error.outcome);
    return new CuffSendError(
      reason,
      error.status,
      message ?? error.message,
      error.outcome,
      error.retryAfterSeconds,
    );
  }
}

/** "field: fragment; other: fragment" from an OperationOutcome's issues, or null if none. */
export function issueMessage(outcome: OperationOutcome | null): string | null {
  const parts = (outcome?.issue ?? []).flatMap((i) => {
    const text = i.diagnostics?.trim() ?? '';
    const where = (i.expression ?? []).join(', ');
    if (text === '') return where === '' ? [] : [where];
    return [where === '' ? text : `${where}: ${text}`];
  });
  return parts.length > 0 ? parts.join('; ') : null;
}

/**
 * Seconds from a `Retry-After` header. Only the delay-seconds form (RFC 9110 section 10.2.3:
 * one or more digits) is accepted; anything else (a negative or decimal number, `10abc`, or the
 * HTTP-date form, which neither cuff-sim nor the login route sends) is null, "not given".
 */
export function retryAfterSeconds(header: string | null): number | null {
  if (header === null) return null;
  const value = header.trim();
  return /^\d+$/.test(value) ? Number(value) : null;
}
