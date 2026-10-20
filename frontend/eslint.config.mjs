import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import jsxA11y from "eslint-plugin-jsx-a11y";

// Accessibility (P2.10, docs/accessibility.md): eslint-config-next turns on only 6 jsx-a11y rules, as warnings.
// Here the plugin's whole recommended set is an error for frontend/src, with the plugin's own options. The
// recommended set leaves two rules off (anchor-ambiguous-text and control-has-associated-label) and one
// deprecated rule (label-has-for); docs/accessibility.md says why they stay off. eslint-config-next already
// registers the plugin under the name jsx-a11y, so only the rules are added here.
const a11yRecommended = Object.fromEntries(
  Object.entries(jsxA11y.flatConfigs.recommended.rules).map(([rule, setting]) => {
    const [level, ...options] = Array.isArray(setting) ? setting : [setting];
    return [rule, level === "off" ? "off" : ["error", ...options]];
  }),
);
// The shadcn/ui form controls render native inputs: a label wrapped around one of them is associated with it.
a11yRecommended["jsx-a11y/label-has-associated-control"] = [
  "error",
  { controlComponents: ["Input", "Textarea", "Checkbox", "Switch", "RadioGroupItem", "SelectTrigger"], depth: 3 },
];

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    files: ["src/**/*.{js,jsx,ts,tsx}"],
    rules: a11yRecommended,
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
