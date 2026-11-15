import { describe, expect, it } from 'vitest';
import { codeBlock, el } from './dom';

describe('el()', () => {
  it('renders strings as text, never as markup', () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const node = el('p', {}, hostile);
    expect(node.textContent).toBe(hostile);
    expect(node.querySelector('img')).toBeNull();
  });

  it('codeBlock shows ER7 segments on separate lines', () => {
    const block = codeBlock('MSH|^~\\&|A\rPID|1', 'HL7v2');
    expect(block.textContent).toBe('MSH|^~\\&|A\nPID|1');
  });
});
