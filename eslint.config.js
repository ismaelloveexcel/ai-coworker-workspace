// @ts-check
const tseslint = require('typescript-eslint');

module.exports = tseslint.config(
  {
    ignores: ['node_modules/**', 'eslint.config.js', '.next/**', 'next-env.d.ts', 'app/**', 'components/**'],
  },
  {
    files: ['**/*.ts', '**/*.tsx'],
    extends: [...tseslint.configs.recommended],
  },
  {
    files: ['**/*.js', '**/*.cjs', '**/*.mjs'],
    rules: {
      '@typescript-eslint/no-require-imports': 'off',
    },
  }
);
