// ESLint flat config: the recommended JavaScript rules plus typescript-eslint's type-aware
// "strict" set. Formatting is Prettier's job, so no stylistic rules here.
import js from '@eslint/js';
import noUnsanitized from 'eslint-plugin-no-unsanitized';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['node_modules/', 'dist/', 'dist-mock/', 'test-results/', 'playwright-report/'] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  // Mozilla's rules against assigning or passing unsanitized strings to HTML sinks
  // (innerHTML, outerHTML, insertAdjacentHTML, document.write, createContextualFragment, ...).
  noUnsanitized.configs.recommended,
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
      // Catch the ways around the property ban above: an object literal or computed key named
      // after an HTML sink (Object.assign(node, { innerHTML }), node['innerHTML']), Reflect.set,
      // the HTML-parsing methods, inline event-handler attributes and javascript: URLs.
      // src/ui/dom.ts also refuses on* attributes and unsafe URLs at run time.
      'no-restricted-syntax': [
        'error',
        {
          selector:
            'Property[key.name=/^(innerHTML|outerHTML|srcdoc)$/], Property[key.value=/^(innerHTML|outerHTML|srcdoc)$/]',
          message: 'Do not set HTML through an object literal; render text with el().',
        },
        {
          selector:
            'MemberExpression[computed=true][property.value=/^(innerHTML|outerHTML|srcdoc)$/]',
          message: 'Do not set HTML through a computed key; render text with el().',
        },
        {
          selector:
            "MemberExpression[object.name='Reflect'][property.name=/^(set|defineProperty)$/]",
          message: 'Reflect.set can write HTML sinks unnoticed; assign normal properties instead.',
        },
        {
          selector:
            'CallExpression[callee.property.name=/^(setHTMLUnsafe|createContextualFragment|parseFromString|parseHTMLUnsafe)$/]',
          message: 'This parses a string as HTML. Build nodes with el() instead.',
        },
        {
          selector:
            'CallExpression[callee.property.name=/^setAttribute(NS)?$/] > Literal[value=/^\\s*on/i]',
          message: 'No inline event-handler attributes; use props.on / addEventListener.',
        },
        {
          selector: 'MemberExpression[property.name=/^(srcdoc)$/]',
          message: 'iframe srcdoc renders markup; there is no reason for it on this page.',
        },
        {
          selector:
            'Literal[value=/^\\s*javascript:/i], TemplateElement[value.raw=/^\\s*javascript:/i]',
          message: 'No javascript: URLs.',
        },
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
