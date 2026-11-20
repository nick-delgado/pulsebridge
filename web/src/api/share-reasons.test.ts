import { describe, expect, it } from 'vitest';
import { ApiError, PROXY_REASON_SYSTEM, ShareError } from './errors';
import type { OperationOutcome } from './fhir';

/** The proxy's OperationOutcome for one reason code, as fhir-proxy's Problem builds it. */
function outcome(reason: string): OperationOutcome {
  return {
    resourceType: 'OperationOutcome',
    issue: [
      {
        severity: 'error',
        code: 'transient',
        details: { coding: [{ system: PROXY_REASON_SYSTEM, code: reason }] },
        diagnostics: `text for ${reason}`,
      },
    ],
  };
}

describe('share reasons added by T15', () => {
  it('share-busy (503, no steps) is recognised and means nothing was sent', () => {
    const e = ShareError.from(new ApiError(503, 'busy', outcome('share-busy')));
    expect(e.reason).toBe('share-busy');
    expect(e.nothingSent).toBe(true);
  });

  it('share-internal-error (502 with steps) is recognised and never means nothing was sent', () => {
    const e = ShareError.from(new ApiError(502, 'bug', outcome('share-internal-error')));
    expect(e.reason).toBe('share-internal-error');
    expect(e.nothingSent).toBe(false);
  });
});

describe('share reasons added by T45', () => {
  it('rate-limited (429 from T46, no steps) keeps the wait and means nothing was sent', () => {
    const e = ShareError.from(new ApiError(429, 'wait', outcome('rate-limited'), 30));
    expect([e.reason, e.retryAfterSeconds, e.nothingSent]).toEqual(['rate-limited', 30, true]);
  });
});
