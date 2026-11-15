/**
 * The one way the page builds DOM. Strings passed as children become text nodes, never markup,
 * so a value from the server (an OperationOutcome message, an HL7v2 segment, a patient name)
 * cannot inject HTML or script. ESLint forbids `innerHTML` and friends (eslint.config.js).
 */

type Child = Node | string | number | null | undefined | false;

/** Attributes that load or navigate to a URL. Their values must pass `isSafeUrl`. */
const URL_ATTRIBUTES = new Set(['href', 'src', 'xlink:href', 'action', 'formaction', 'poster']);
/** Attributes that take markup or script whatever their value. */
const FORBIDDEN_ATTRIBUTES = new Set(['srcdoc']);

/**
 * True for a relative URL, a fragment (`#id`) or an `https:` URL. False for `javascript:`,
 * `data:`, `http:` and every other scheme. The URL parser strips the tabs and newlines that
 * browsers ignore, so `java\tscript:` is caught too.
 */
export function isSafeUrl(value: string): boolean {
  try {
    return new URL(value, 'https://relative.invalid/').protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Throws for an attribute that could run script: any `on…` event handler, `srcdoc`, or a URL
 * attribute whose value is not safe. Handlers belong in `props.on` (addEventListener).
 */
export function assertSafeAttribute(name: string, value: string): void {
  const lower = name.toLowerCase();
  if (lower.startsWith('on')) {
    throw new Error(`Refusing attribute ${name}: use props.on for event listeners.`);
  }
  if (FORBIDDEN_ATTRIBUTES.has(lower)) throw new Error(`Refusing attribute ${name}.`);
  if (URL_ATTRIBUTES.has(lower) && !isSafeUrl(value)) {
    throw new Error(`Refusing ${name}: only relative, #fragment or https: URLs are allowed.`);
  }
}

export interface ElementProps {
  class?: string;
  id?: string;
  text?: string;
  /** Attributes, set with setAttribute (aria-*, data-*, type, for, ...). */
  attrs?: Record<string, string | boolean | undefined>;
  /** Event listeners. Removed automatically when `signal` aborts. */
  on?: { [K in keyof HTMLElementEventMap]?: (event: HTMLElementEventMap[K]) => void };
  signal?: AbortSignal;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: ElementProps = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (props.class) node.className = props.class;
  if (props.id) node.id = props.id;
  if (props.text !== undefined) node.textContent = props.text;
  for (const [name, value] of Object.entries(props.attrs ?? {})) {
    if (value === undefined || value === false) continue;
    const text = value === true ? '' : value;
    assertSafeAttribute(name, text);
    node.setAttribute(name, text);
  }
  for (const [type, listener] of Object.entries(props.on ?? {})) {
    const options = props.signal ? { signal: props.signal } : undefined;
    node.addEventListener(type, listener as EventListener, options);
  }
  append(node, ...children);
  return node;
}

export function append(parent: Node, ...children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
}

/** Removes every child of `node`. */
export function clear(node: Node): void {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** Replaces the children of `node`. */
export function replaceChildren(node: Element, ...children: Child[]): void {
  clear(node);
  append(node, ...children);
}

const SVG_NS = 'http://www.w3.org/2000/svg';

/** An SVG element with attributes; used for the silhouette and available to the chart. */
export function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number> = {},
  ...children: (SVGElement | null)[]
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    assertSafeAttribute(name, String(value));
    node.setAttribute(name, String(value));
  }
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

let idCounter = 0;
/** A unique id for aria-labelledby / aria-controls pairs. */
export function uniqueId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-${String(idCounter)}`;
}

/** A pre block for JSON, ER7 or any raw text. ER7 segment separators ("\r") become line breaks. */
export function codeBlock(content: string | object, label: string): HTMLPreElement {
  const text =
    typeof content === 'string'
      ? content.replace(/\r\n?/g, '\n')
      : JSON.stringify(content, null, 2);
  return el(
    'pre',
    { class: 'code-block', attrs: { tabindex: '0', 'aria-label': label } },
    el('code', { text }),
  );
}

/** A muted "nothing here yet" message for an empty region. */
export function placeholder(text: string): HTMLParagraphElement {
  return el('p', { class: 'placeholder', text });
}
