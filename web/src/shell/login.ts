/**
 * The login view (D9): one password field, no username, no self-signup. The form is handled in
 * script; `method="post"` and the CSP's `form-action 'none'` make sure that, if script ever failed,
 * the browser would not fall back to a GET that puts the password in the address bar.
 */
import type { PulseBridgeApi } from '../api/contract';
import { LoginError } from '../api/errors';
import { el } from '../ui/dom';

export interface LoginViewOptions {
  client: PulseBridgeApi;
  /** Shown above the form, for example after the session ended. */
  notice?: string;
  onSuccess: () => void;
}

export function loginView(options: LoginViewOptions): HTMLElement {
  const { client } = options;
  const input = el('input', {
    id: 'login-password',
    class: 'login__input',
    attrs: {
      type: 'password',
      name: 'password',
      autocomplete: 'current-password',
      required: true,
      'aria-describedby': 'login-error',
    },
  });
  const error = el('p', { id: 'login-error', class: 'login__error', attrs: { role: 'alert' } });
  const submit = el('button', {
    class: 'button button--primary',
    text: 'Log in',
    attrs: { type: 'submit' },
  });

  const form = el(
    'form',
    {
      class: 'login__form',
      attrs: { method: 'post', 'aria-label': 'Reviewer login', novalidate: true },
      on: {
        submit: (event) => {
          event.preventDefault();
          void attempt();
        },
      },
    },
    el('label', { class: 'login__label', text: 'Password', attrs: { for: 'login-password' } }),
    input,
    error,
    submit,
  );

  async function attempt(): Promise<void> {
    const password = input.value;
    if (password === '') {
      error.textContent = 'Enter the reviewer password.';
      input.focus();
      return;
    }
    submit.disabled = true;
    error.textContent = '';
    try {
      await client.login(password);
      input.value = '';
      options.onSuccess();
    } catch (e) {
      error.textContent = e instanceof LoginError ? e.message : 'Login failed. Try again.';
      input.select();
    } finally {
      submit.disabled = false;
    }
  }

  return el(
    'main',
    { class: 'login', id: 'main', attrs: { 'aria-labelledby': 'login-title' } },
    el(
      'div',
      { class: 'login__card' },
      el('h1', { id: 'login-title', class: 'login__title', text: 'PulseBridge' }),
      el('p', {
        class: 'login__intro',
        text: 'A personal health record demo: heart rate from a watch, blood pressure from a simulated cuff, shared with a clinic over FHIR. Enter the reviewer password to continue.',
      }),
      options.notice
        ? el('p', { class: 'login__notice', text: options.notice, attrs: { role: 'status' } })
        : null,
      client.mode === 'mock'
        ? el('p', {
            class: 'login__mock',
            text: 'Mock mode: synthetic data and no server. Any non-empty password logs in.',
          })
        : null,
      form,
    ),
  );
}
