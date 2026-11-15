// ESLint flat config: the recommended JavaScript rules plus typescript-eslint's type-aware
// "strict" set. Formatting is Prettier's job, so no stylistic rules here.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['node_modules/', 'dist/', 'dist-mock/', 'test-results/', 'playwright-report/'] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      // Security convention for every task (README, "Rendering data"): data reaches the page as
      // text, never as HTML. These properties and calls parse a string as markup.
      'no-restricted-properties': [
        'error',
        { property: 'innerHTML', message: 'Render data with textContent or el() (src/ui/dom.ts).' },
        { property: 'outerHTML', message: 'Render data with textContent or el() (src/ui/dom.ts).' },
        { property: 'insertAdjacentHTML', message: 'Render data with el() (src/ui/dom.ts).' },
        { object: 'document', property: 'write', message: 'Never use document.write.' },
      ],
      // A template literal in a message is fine for numbers; forbid only things that stringify badly.
      '@typescript-eslint/restrict-template-expressions': ['error', { allowNumber: true }],
    },
  },
  {
    // Plain Node scripts (the bundle check) are not part of the TypeScript project.
    files: ['**/*.js', '**/*.mjs'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: {
      parserOptions: { projectService: false },
      globals: { process: 'readonly', console: 'readonly', URL: 'readonly' },
    },
  },
);
