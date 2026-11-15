/**
 * "Sent" versus "As stored in OpenEMR" (D13): the differences between the resource the proxy
 * sent and the one OpenEMR returned when read back by id, in words. Pure (diff.test.ts).
 *
 * OpenEMR is not a FHIR store that keeps what it is given: it files a reading into its own
 * Vitals form and builds a FHIR view of that row when asked (spike S1). So what comes back can
 * lack elements that were sent, carry elements OpenEMR adds (its own `id`, `meta.profile`), and
 * hold rewritten texts and times. The panel says each of those plainly, because "the clinic has
 * my reading" should mean "the clinic has THIS", not "something with a 201".
 */

export interface Difference {
  /** FHIRPath-like location, for example `code.coding[0].display`. */
  path: string;
  change: 'dropped' | 'added' | 'changed';
  sent: unknown;
  stored: unknown;
}

/** How many differences one resource lists before "and N more". */
export const MAX_DIFFERENCES = 40;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function join(path: string, key: string): string {
  return path === '' ? key : `${path}.${key}`;
}

/**
 * Every difference between `sent` and `stored`, depth first, in the order of the sent resource's
 * elements (then those only OpenEMR added). Objects are compared element by element, arrays
 * item by item; an element present on one side only is reported once, at its own path.
 */
export function differences(sent: unknown, stored: unknown, path = ''): Difference[] {
  if (isObject(sent) && isObject(stored)) {
    const out: Difference[] = [];
    for (const key of Object.keys(sent)) {
      const p = join(path, key);
      if (!(key in stored))
        out.push({ path: p, change: 'dropped', sent: sent[key], stored: undefined });
      else out.push(...differences(sent[key], stored[key], p));
    }
    for (const key of Object.keys(stored)) {
      if (!(key in sent)) {
        out.push({ path: join(path, key), change: 'added', sent: undefined, stored: stored[key] });
      }
    }
    return out;
  }
  if (Array.isArray(sent) && Array.isArray(stored)) {
    const out: Difference[] = [];
    const n = Math.max(sent.length, stored.length);
    for (let i = 0; i < n; i++) {
      const p = `${path}[${String(i)}]`;
      if (i >= stored.length)
        out.push({ path: p, change: 'dropped', sent: sent[i], stored: undefined });
      else if (i >= sent.length)
        out.push({ path: p, change: 'added', sent: undefined, stored: stored[i] });
      else out.push(...differences(sent[i], stored[i], p));
    }
    return out;
  }
  return Object.is(sent, stored) ? [] : [{ path, change: 'changed', sent, stored }];
}

/** A value as short text: JSON, cut at 80 characters. */
export function shortValue(value: unknown): string {
  // JSON.stringify gives undefined for undefined (its type says string).
  const json = JSON.stringify(value) as string | undefined;
  const text = json ?? String(value);
  return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}

/** One difference as a sentence. */
export function differenceText(d: Difference): string {
  switch (d.change) {
    case 'dropped':
      return `${d.path} was sent, but OpenEMR did not keep it (sent: ${shortValue(d.sent)}).`;
    case 'added':
      return `${d.path} was added by OpenEMR (stored: ${shortValue(d.stored)}).`;
    case 'changed':
      return `${d.path} changed: sent ${shortValue(d.sent)}, stored ${shortValue(d.stored)}.`;
  }
}

/** The sentences for a pair, bounded, with a count of the rest. */
export function differenceSentences(sent: unknown, stored: unknown): string[] {
  const all = differences(sent, stored);
  const shown = all.slice(0, MAX_DIFFERENCES).map(differenceText);
  if (all.length > MAX_DIFFERENCES) shown.push(`And ${String(all.length - MAX_DIFFERENCES)} more.`);
  return shown;
}
