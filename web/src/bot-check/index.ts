/**
 * The bot check the landing view runs before asking for a demo session (D16).
 *
 * In an HTTP build it is Google's reCAPTCHA (Google Cloud Fraud Defense, formerly reCAPTCHA
 * Enterprise) with a score-based key: invisible, no puzzle. The browser script watches how the
 * page is used and, on `execute`, returns a short-lived token; the proxy sends the token to
 * Google in an assessment and gets back a score from 0.0 (very likely a bot) to 1.0 (very likely
 * a person). The page never sees the score: the proxy decides.
 *
 * In a mock build (`npm run dev`, the browser tests) it is a stand-in that loads nothing, so no
 * Google key and no network are needed. The mode is a build-time constant, so an HTTP build does
 * not contain the stand-in (scripts/check-bundle.mjs checks).
 */
import type { AuthConfig } from '../api/contract';
import { RecaptchaBotCheck } from './recaptcha';

export interface BotCheck {
  /**
   * Loads the provider's script for this key (once) and waits until it is ready. Called as soon
   * as the landing view has the config, so the script has the visit to observe, not only the
   * click. Rejects with `BotCheckLoadError` if the script cannot load or start.
   */
  prepare(config: AuthConfig): Promise<void>;
  /** A fresh token for `config.recaptchaAction` (tokens are short-lived: ask on each click). */
  token(config: AuthConfig): Promise<string>;
}

/** The provider's script did not load or did not start (blocked, offline, or broken). */
export class BotCheckLoadError extends Error {
  override readonly name = 'BotCheckLoadError';
}

export async function createBotCheck(): Promise<BotCheck> {
  // Compare import.meta.env.MODE here (as api/index.ts does), so the bundler drops the branch.
  if (import.meta.env.MODE === 'mock') {
    const { MockBotCheck } = await import('./mock');
    return new MockBotCheck();
  }
  return new RecaptchaBotCheck();
}
