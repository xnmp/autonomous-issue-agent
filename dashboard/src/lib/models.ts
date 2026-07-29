/** Keep model chips narrow; the full identifier remains in the title. */
export function shortModel(model: string): string {
  return model
    .replace(/^(claude|openai|anthropic)[-/]/, "")
    .replace(/\[1m\]$/, "");
}
