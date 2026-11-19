/**
 * The landing view (D16): what the demo is, one "View the live demo" button, and the reCAPTCHA
 * notice. No password, no account. The flow, with the contract's routes (under `/api`):
 *
 *   render ── GET /auth/config ──▶ {site key, action} ──▶ load Google's enterprise.js (prepare)
 *   click ── grecaptcha.enterprise.execute(key, {action}) ──▶ token (assessed within 2 min)
 *         ── POST /auth/session {recaptcha_token} ──▶ 200 + Set-Cookie __session ──▶ dashboard
 *                                                 └─▶ 403/429/503 ──▶ a message, stay here
 *
 * The site key is fetched, never written into the page, so a stranger's deployment uses its own
 * key. The script is loaded only once the key is known, and as soon as it is, so reCAPTCHA can
 * observe the visit before the click (its score is better for it).
 *
 * The badge Google's script shows in the corner is hidden (`.grecaptcha-badge`, layout.css).
 * Google's FAQ (https://docs.cloud.google.com/recaptcha/docs/faq, read directly on
 * 19 November 2026) allows that when the page says it uses reCAPTCHA, with exactly the text
 * "This site is protected by reCAPTCHA.": the notice under the button. Since 2 April 2026 the FAQ
 * no longer asks for links to Google's Privacy Policy and Terms (the site owner is the data
 * controller, Google the processor), so there are none.
 */
import type { PulseBridgeApi, AuthConfig } from '../api/contract';
import { SESSION_REFUSALS, SessionError } from '../api/errors';
import { BotCheckLoadError, type BotCheck } from '../bot-check/index';
import { el } from '../ui/dom';

export const START_LABEL = 'View the live demo';
export const STARTING_LABEL = 'Starting the demo…';

/** The wording Google's FAQ gives for a page that hides the badge. */
export const RECAPTCHA_NOTICE = 'This site is protected by reCAPTCHA.';

export const DEMO_INTRO =
  'PulseBridge is a personal health record built on FHIR, HL7v2 and Terraform: real heart-rate data from a watch and blood-pressure readings from a simulated cuff, stored as FHIR and shared with a test clinic. The live demo shows the real readings, and you can send cuff readings and share them yourself; there is nothing to sign up for.';

export interface LandingViewOptions {
  client: PulseBridgeApi;
  botCheck: BotCheck;
  /** Shown above the button, for example after the session ended. */
  notice?: string;
  onSuccess: () => void;
}

export function landingView(options: LandingViewOptions): HTMLElement {
  const { client, botCheck } = options;

  // The config is asked for at once and kept; a failed fetch is retried on the next click.
  let config: Promise<AuthConfig> | null = null;
  const loadConfig = (): Promise<AuthConfig> => {
    if (config === null) {
      const pending = client.getAuthConfig();
      config = pending;
      pending.catch(() => {
        if (config === pending) config = null;
      });
      // Start loading Google's script now; a failure is reported on the click, not before.
      pending.then((c) => botCheck.prepare(c)).catch(() => undefined);
    }
    return config;
  };

  const error = el('p', {
    id: 'landing-error',
    class: 'landing__error',
    attrs: { role: 'alert' },
  });
  const button = el('button', {
    id: 'landing-start',
    class: 'button button--primary landing__start',
    text: START_LABEL,
    attrs: { type: 'button', 'aria-describedby': 'landing-error' },
    on: {
      click: () => {
        void start();
      },
    },
  });

  async function start(): Promise<void> {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = STARTING_LABEL;
    error.textContent = '';
    try {
      const c = await loadConfig();
      // A fresh token on each click, posted at once: the proxy must assess it within two minutes.
      const token = await botCheck.token(c);
      await client.startSession(token);
      options.onSuccess();
    } catch (e) {
      error.textContent = failureText(e);
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.textContent = START_LABEL;
    }
    // Disabling the button dropped keyboard focus; after a failure put it back for another try
    // (the alert above it has already been announced).
    if (button.isConnected && error.textContent !== '') button.focus();
  }

  // At once, not on the click: the script then has the visit to observe. Failures are handled
  // inside loadConfig and reported on the click.
  void loadConfig().catch(() => undefined);

  return el(
    'main',
    { class: 'landing', id: 'main', attrs: { 'aria-labelledby': 'landing-title' } },
    el(
      'div',
      { class: 'landing__card' },
      el('h1', { id: 'landing-title', class: 'landing__title', text: 'PulseBridge' }),
      el('p', { class: 'landing__intro', text: DEMO_INTRO }),
      options.notice
        ? el('p', { class: 'landing__notice', text: options.notice, attrs: { role: 'status' } })
        : null,
      client.mode === 'mock'
        ? el('p', {
            class: 'landing__mock',
            text: 'Mock mode: synthetic data and no server. A stand-in replaces the bot check and no Google script is loaded.',
          })
        : null,
      button,
      error,
      el('p', { class: 'landing__recaptcha', text: RECAPTCHA_NOTICE }),
    ),
  );
}

/** The visitor's sentence for a failed start. */
function failureText(error: unknown): string {
  if (error instanceof SessionError) return error.message;
  if (error instanceof BotCheckLoadError) return SESSION_REFUSALS['bot-check-not-loaded'];
  return 'The demo could not start. Try again.';
}
