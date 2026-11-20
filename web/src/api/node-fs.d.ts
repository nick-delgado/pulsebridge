/**
 * The one Node API the drift tests use (reasons.test.ts reads files from other parts of the
 * repository). @types/node is not a dependency, so only this signature is declared. Unit tests
 * only: the page never imports `node:fs`, and the bundle check would show it if it did.
 */
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string;
  /** With `recursive`, relative paths of every entry below `path` (src/shell/privacy.test.ts). */
  export function readdirSync(path: string, options: { recursive: true }): string[];
}
