import { describe, expect, it } from 'vitest';
import { rateLimitMessage } from './errors';

describe('rateLimitMessage: the wait from Retry-After (T46, any route)', () => {
  it.each([
    [1, 'Too many requests. Wait 1 second, then try again.'],
    [30, 'Too many requests. Wait 30 seconds, then try again.'],
    // Seconds below two minutes, whole minutes rounded up from 120.
    [119, 'Too many requests. Wait 119 seconds, then try again.'],
    [120, 'Too many requests. Wait 2 minutes, then try again.'],
    [121, 'Too many requests. Wait 3 minutes, then try again.'],
    [null, 'Too many requests. Wait a minute, then try again.'],
  ])('%s seconds reads "%s"', (seconds, text) => {
    expect(rateLimitMessage(seconds)).toBe(text);
  });
});
