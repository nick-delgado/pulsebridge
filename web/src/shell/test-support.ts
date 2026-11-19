/** Helpers for the shell's unit tests (not shipped: only tests import this file). */
import { MockBotCheck } from '../bot-check/mock';
import type { BotCheck } from '../bot-check/index';
import { START_LABEL } from './landing';

/** Lets pending promise callbacks (mock latency 0, fake fetch) run. */
export const settle = async (rounds = 5): Promise<void> => {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 0));
};

/** The mock build's bot check: no script, a token the mock client accepts. */
export const mockBotCheck = (): BotCheck => new MockBotCheck();

/** The landing view's start button in `root`, or null. */
export function startButton(root: ParentNode): HTMLButtonElement | null {
  return [...root.querySelectorAll('button')].find((b) => b.textContent === START_LABEL) ?? null;
}

/** Clicks "View the live demo" and lets the flow run. */
export async function viewDemo(root: ParentNode): Promise<void> {
  const button = startButton(root);
  if (!button) throw new Error('no "View the live demo" button');
  button.click();
  await settle();
}

/** Clicks "Log out". */
export function logOut(root: ParentNode): void {
  [...root.querySelectorAll('button')].find((b) => b.textContent === 'Log out')?.click();
}
