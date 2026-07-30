export function buildDirectives(draft) {
  const lines = [];
  for (const id of draft.removedThemes) lines.push(`REMOVE THEME: ${id}`);
  for (const id of draft.addedThemes) lines.push(`ADD THEME: ${id}`);
  for (const id of draft.removedGaps) lines.push(`REMOVE GAP ${id}`);
  for (const [id, text] of draft.correctedGaps) {
    lines.push(`CORRECT GAP ${id}: ${text}`);
  }
  for (const text of draft.newGaps) lines.push(`ADD GAP: ${text}`);
  for (const [id, text] of draft.seminalOverrides) {
    lines.push(`OVERRIDE SEMINAL ${id}: ${text}`);
  }
  for (const [id, text] of draft.verdictOverrides) {
    lines.push(`OVERRIDE VERDICT ${id}: ${text}`);
  }
  for (const output of draft.outputs) {
    lines.push(`SCRIBE OUTPUT: ${output.type} | audience: ${output.audience}`);
  }
  if (!lines.length && !draft.freeText.trim()) lines.push('CONFIRMED');
  return lines;
}
