module.exports = {
  root: true,
  env: {
    browser: true,
    es2021: true,
    node: true,
  },
  extends: [
    'eslint:recommended',
    // Keep the first adoption focused on correctness. The full recommended
    // preset also enforces thousands of formatting rules against the existing
    // templates, drowning out actionable findings.
    'plugin:vue/vue3-essential',
    'plugin:@typescript-eslint/recommended',
  ],
  parser: 'vue-eslint-parser',
  parserOptions: {
    parser: '@typescript-eslint/parser',
    ecmaVersion: 'latest',
    sourceType: 'module',
  },
  plugins: ['vue', '@typescript-eslint'],
  rules: {
    'vue/multi-word-component-names': 'off',
    // Existing legacy patterns stay advisory so `npm run lint` and the
    // pre-commit hook are usable while the baseline is paid down gradually.
    'no-empty': ['warn', { allowEmptyCatch: true }],
    'no-irregular-whitespace': 'warn',
    'no-constant-condition': 'warn',
    'no-extra-semi': 'warn',
    'no-inner-declarations': 'warn',
    'vue/no-ref-as-operand': 'warn',
    '@typescript-eslint/no-empty-object-type': 'warn',
    '@typescript-eslint/no-unused-expressions': 'warn',
    '@typescript-eslint/no-explicit-any': 'warn',
    '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
  },
};
