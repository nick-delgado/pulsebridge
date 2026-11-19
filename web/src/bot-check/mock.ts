/**
 * The mock build's stand-in for reCAPTCHA: loads no script and returns a token the mock client
 * recognises. Whether the "assessment" passes is the mock client's choice (`?mock-demo=`), as it
 * is the proxy's in a real deployment.
 */
import type { AuthConfig } from '../api/contract';
import type { BotCheck } from './index';

/** Mock tokens look nothing like Google's on purpose. The mock client accepts only these. */
export const MOCK_RECAPTCHA_TOKEN_PREFIX = 'mock-recaptcha-';

export class MockBotCheck implements BotCheck {
  #count = 0;

  prepare(): Promise<void> {
    return Promise.resolve();
  }

  token(config: AuthConfig): Promise<string> {
    this.#count++;
    return Promise.resolve(
      `${MOCK_RECAPTCHA_TOKEN_PREFIX}${config.recaptchaAction}-${this.#count}`,
    );
  }
}
