import type { ShareFailureReason, ShareStep } from './contract';
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
 * The code system fhir-proxy puts its own reason codes in: every error body it sends is an
 * OperationOutcome whose `issue[0].details.coding[0]` has this system and a fixed reason code
 * (services/fhir-proxy/README.md, "Routes as built").
 */
export const PROXY_REASON_SYSTEM = 'urn:pulsebridge:fhir-proxy:reason';

/** The proxy's reason code in an OperationOutcome (for example `login-not-configured`), or null. */
export function proxyReason(outcome: OperationOutcome | null): string | null {
  for (const issue of outcome?.issue ?? []) {
    for (const coding of issue.details?.coding ?? []) {
      if (coding.system === PROXY_REASON_SYSTEM && typeof coding.code === 'string') {
        return coding.code;
      }
    }
  }
  return null;
}

/**
 * True for the proxy's 503 that means its reviewer-password secret is missing (amendment 11):
 * then EVERY route answers 503 `login-not-configured`, because no token can be checked. The
 * session is not over (no 401): the page keeps it and says the service is not configured.
 */
export function isServiceNotConfigured(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 503 &&
    proxyReason(error.outcome) === 'login-not-configured'
  );
}

/**
 * True for a trace answer that retrying cannot change (amendment 12): 404 (no such record, or
 * another patient's), 410 (gone), and 422 (the AuditEvent is ours but unreadable).
 */
export function isFinalTraceAnswer(error: unknown): boolean {
  return error instanceof ApiError && [404, 410, 422].includes(error.status);
}

/** The statuses the proxy uses for a share refused before OpenEMR is called. */
const NOTHING_SENT_STATUSES: readonly number[] = [400, 404, 422, 503];

/** The share's documented reason codes (contract.ts `ShareFailureReason`). */
const SHARE_REASONS: readonly ShareFailureReason[] = [
  'invalid-body',
  'not-found',
  'observation-not-final',
  'observation-not-shareable',
  'patient-incomplete',
  'openemr-unreachable',
  'openemr-refused',
  'openemr-timeout',
  'share-not-configured',
];

/**
 * A share that did not end with the reading in OpenEMR. `reason` is the proxy's code (`other`
 * when it sent none this page knows); `steps` are the steps completed before it stopped, empty
 * when it was refused before OpenEMR was called (400, 404, 422, 503).
 */
export class ShareError extends ApiError {
  override readonly name: string = 'ShareError';
  constructor(
    readonly reason: ShareFailureReason,
    status: number,
    message: string,
    outcome: OperationOutcome | null,
    readonly steps: ShareStep[],
  ) {
    super(status, message, outcome);
  }

  /**
   * True ONLY when the proxy refused the share before calling OpenEMR: a 400, 404, 422 or 503
   * whose body is the proxy's OperationOutcome, with no steps. Anything else (no connection, a
   * 5xx without the steps envelope, an HTML error page from the hosting layer, a 200 the page
   * could not read) may come after the proxy had already created a Patient, an Encounter or the
   * reading at the clinic, so it is not known what reached it.
   */
  get nothingSent(): boolean {
    return (
      this.steps.length === 0 &&
      this.outcome !== null &&
      NOTHING_SENT_STATUSES.includes(this.status)
    );
  }

  /** The typed error for an `ApiError` from the share route, with the steps the body carried. */
  static from(error: ApiError, steps: ShareStep[] = []): ShareError {
    const code = proxyReason(error.outcome);
    const reason = SHARE_REASONS.find((r) => r === code) ?? 'other';
    return new ShareError(reason, error.status, error.message, error.outcome, steps);
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
