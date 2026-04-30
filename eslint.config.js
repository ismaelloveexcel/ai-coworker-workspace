// @ts-check
const tseslint = require('typescript-eslint');

module.exports = tseslint.config(
  ...tseslint.configs.recommended,
  {
    ignores: ['node_modules/**', 'eslint.config.js'],
  }
);
