/**
 * reCAPTCHA (Google Cloud Fraud Defense) with a score-based web key, as Google's guide "Install
 * score-based keys on websites" describes it
 * (https://docs.cloud.google.com/recaptcha/docs/instrument-web-pages, read directly on
 * 19 November 2026; page last updated 2026-11-10). Its example, for a login action:
 *
 *   <script src="https://www.google.com/recaptcha/enterprise.js?render=KEY_ID"></script>
 *   grecaptcha.enterprise.ready(async () => {
 *     const token = await grecaptcha.enterprise.execute('KEY_ID', {action: 'LOGIN'});
 *   });
 *
 * - `ready` runs its callback once the library has loaded, so `execute` is never called early.
 * - `execute` is called on the interaction being protected (here the "View the live demo"
 *   click) with a meaningful action name; the proxy's assessment checks the token's action and
 *   hostname against what it expects (`view_demo`, the page's hosts).
 * - The guide says to create the assessment within two minutes of the token being made, so one
 *   is asked for on each click, posted at once, and never kept.
 *
 * Where this differs from the guide: the guide puts the script in the page's <head>. Here it is
 * added only after `GET /auth/config` answered, because the key is not in the page or its build
 * (no hardcoded identity). The landing view calls `prepare` as soon as it renders, so the script
 * still runs for the whole visit to the landing view, not only at the click.
 *
 * The page's Content-Security-Policy must allow the script and its frame (vite.config.ts and the
 * hosting header, T44).
 */
import type { AuthConfig } from '../api/contract';
import { el } from '../ui/dom';
import { BotCheckLoadError, type BotCheck } from './index';

/** The part of Google's `grecaptcha.enterprise` API this page uses. */
interface RecaptchaEnterprise {
  ready(callback: () => void): void;
  execute(siteKey: string, options: { action: string }): Promise<string>;
}

interface RecaptchaWindow {
  grecaptcha?: { enterprise?: RecaptchaEnterprise };
}

export const RECAPTCHA_SCRIPT = 'https://www.google.com/recaptcha/enterprise.js';

/** How long the script may take to load and become ready before the page gives up. */
export const LOAD_TIMEOUT_MS = 15_000;

export class RecaptchaBotCheck implements BotCheck {
  #loading: Promise<RecaptchaEnterprise> | null = null;
  #key: string | null = null;

  constructor(private readonly timeoutMs: number = LOAD_TIMEOUT_MS) {}

  async prepare(config: AuthConfig): Promise<void> {
    await this.#load(config.recaptchaSiteKey);
  }

  async token(config: AuthConfig): Promise<string> {
    const api = await this.#load(config.recaptchaSiteKey);
    try {
      const token = await api.execute(config.recaptchaSiteKey, {
        action: config.recaptchaAction,
      });
      if (typeof token !== 'string' || token === '') throw new Error('empty token');
      return token;
    } catch (error) {
      throw new BotCheckLoadError(`reCAPTCHA did not give a token: ${String(error)}`);
    }
  }

  #load(siteKey: string): Promise<RecaptchaEnterprise> {
    // One script per page: `render=` binds it to one key. The key does not change during a visit
    // (it comes from this deployment's config); a different one would need a reload.
    if (this.#loading && this.#key === siteKey) return this.#loading;
    if (this.#loading) {
      return Promise.reject(new BotCheckLoadError('reCAPTCHA is already loaded for another key.'));
    }
    this.#key = siteKey;
    const attempt = new Promise<RecaptchaEnterprise>((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new BotCheckLoadError('reCAPTCHA did not load in time.'));
      }, this.timeoutMs);
      const fail = (message: string) => {
        clearTimeout(timer);
        reject(new BotCheckLoadError(message));
      };
      const script = el('script', {
        attrs: {
          src: `${RECAPTCHA_SCRIPT}?render=${encodeURIComponent(siteKey)}`,
          async: true,
        },
        on: {
          load: () => {
            const api = (window as Window & RecaptchaWindow).grecaptcha?.enterprise;
            if (!api) {
              fail('reCAPTCHA loaded but grecaptcha.enterprise is missing.');
              return;
            }
            api.ready(() => {
              clearTimeout(timer);
              resolve(api);
            });
          },
          error: () => {
            fail('reCAPTCHA could not be loaded.');
          },
        },
      });
      document.head.append(script);
    });
    // A failed load may be retried (the visitor unblocked the script, or came back online).
    this.#loading = attempt.catch((error: unknown) => {
      this.#loading = null;
      this.#key = null;
      for (const s of document.querySelectorAll(`script[src^="${RECAPTCHA_SCRIPT}"]`)) s.remove();
      throw error;
    });
    return this.#loading;
  }
}
