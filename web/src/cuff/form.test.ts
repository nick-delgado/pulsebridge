import { describe, expect, it } from 'vitest';
import {
  checkForm,
  parsePressure,
  plausibilityNote,
  sampleAvailable,
  samplesByClass,
  type CuffFormState,
} from './form';
import { SAMPLES } from './test-support';

const base: CuffFormState = {
  systolic: '120',
  diastolic: '80',
  transport: 'mllp',
  sendMalformed: false,
  sample: '',
};

describe('parsePressure: the simulator limits, 1 to 400 whole mm[Hg]', () => {
  it.each([
    ['1', 1],
    ['400', 400],
    ['120', 120],
    [' 95 ', 95],
  ])('accepts %j as %d', (raw, value) => {
    expect(parsePressure(raw)).toEqual({ value });
  });

  it.each(['0', '401', '1000'])('refuses %j as out of bounds', (raw) => {
    expect(parsePressure(raw)).toEqual({
      error: "Must be from 1 to 400 mm[Hg] (the simulator's limit).",
    });
  });

  it.each(['120.5', '120.0', '1e2', '-5', '+120', '120 mmHg', 'abc', '12,0'])(
    'refuses the non-integer %j',
    (raw) => {
      expect(parsePressure(raw)).toEqual({
        error: 'Enter a whole number of mm[Hg], with no decimals or units.',
      });
    },
  );

  it('refuses an empty field', () => {
    expect(parsePressure('  ')).toEqual({ error: 'Enter a whole number from 1 to 400.' });
  });
});

describe('checkForm: the request body for each form state', () => {
  it('a built reading sends both pressures and sendMalformed null', () => {
    expect(checkForm(base, SAMPLES)).toStrictEqual({
      ok: true,
      request: { systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null },
    });
  });

  it('over HTTPS, at the boundaries 2/1 and 400/399', () => {
    expect(
      checkForm({ ...base, systolic: '2', diastolic: '1', transport: 'https' }, SAMPLES),
    ).toStrictEqual({
      ok: true,
      request: { systolic: 2, diastolic: 1, transport: 'https', sendMalformed: null },
    });
    expect(checkForm({ ...base, systolic: '400', diastolic: '399' }, SAMPLES)).toStrictEqual({
      ok: true,
      request: { systolic: 400, diastolic: 399, transport: 'mllp', sendMalformed: null },
    });
  });

  it('a malformed sample sends no pressures at all, even when they are typed (or invalid)', () => {
    const check = checkForm(
      {
        ...base,
        systolic: '999',
        diastolic: 'x',
        sendMalformed: true,
        sample: 'unparseable_no_msh',
        transport: 'https',
      },
      SAMPLES,
    );
    expect(check).toStrictEqual({
      ok: true,
      request: { transport: 'https', sendMalformed: 'unparseable_no_msh' },
    });
    if (check.ok) {
      expect('systolic' in check.request).toBe(false);
      expect('diastolic' in check.request).toBe(false);
    }
  });

  it('systolic equal to diastolic is refused on systolic', () => {
    expect(checkForm({ ...base, systolic: '80', diastolic: '80' }, SAMPLES)).toStrictEqual({
      ok: false,
      errors: { systolic: 'Must be greater than the diastolic value.' },
    });
  });

  it('systolic below diastolic is refused', () => {
    expect(checkForm({ ...base, systolic: '70', diastolic: '80' }, SAMPLES)).toEqual({
      ok: false,
      errors: { systolic: 'Must be greater than the diastolic value.' },
    });
  });

  it('collects an error per wrong field', () => {
    expect(checkForm({ ...base, systolic: '0', diastolic: '401' }, SAMPLES)).toStrictEqual({
      ok: false,
      errors: {
        systolic: "Must be from 1 to 400 mm[Hg] (the simulator's limit).",
        diastolic: "Must be from 1 to 400 mm[Hg] (the simulator's limit).",
      },
    });
  });

  it('a malformed send needs a chosen sample', () => {
    expect(checkForm({ ...base, sendMalformed: true, sample: '' }, SAMPLES)).toStrictEqual({
      ok: false,
      errors: { sendMalformed: 'Choose a sample to send.' },
    });
    expect(checkForm({ ...base, sendMalformed: true, sample: 'nope' }, SAMPLES)).toStrictEqual({
      ok: false,
      errors: { sendMalformed: 'Choose a sample to send.' },
    });
  });

  it('an MLLP-only sample is refused over HTTPS', () => {
    expect(
      checkForm(
        { ...base, sendMalformed: true, sample: 'noack_missing_end_block', transport: 'https' },
        SAMPLES,
      ),
    ).toStrictEqual({
      ok: false,
      errors: { sendMalformed: 'This sample can only be sent over MLLP.' },
    });
  });
});

describe('sample availability per transport', () => {
  it('bad-framing samples are MLLP only; the others go over both', () => {
    const over = (t: 'mllp' | 'https') =>
      SAMPLES.filter((s) => sampleAvailable(s, t)).map((s) => s.name);
    expect(over('mllp')).toEqual([
      'invalid_missing_pid3',
      'unparseable_no_msh',
      'noack_missing_end_block',
      'unmappable_missing_obx14',
    ]);
    expect(over('https')).toEqual([
      'invalid_missing_pid3',
      'unparseable_no_msh',
      'unmappable_missing_obx14',
    ]);
  });

  it('groups samples by class in the D7 order, and keeps an unknown class', () => {
    const groups = samplesByClass([
      ...SAMPLES.slice().reverse(),
      { name: 'new_one', label: 'x', class: 'brand_new' as never, transports: ['mllp'] },
    ]);
    expect(groups.map((g) => [g.cls, g.samples.map((s) => s.name)])).toEqual([
      ['structurally_invalid', ['invalid_missing_pid3']],
      ['unparseable', ['unparseable_no_msh']],
      ['no_acknowledgment', ['noack_missing_end_block']],
      ['parses_but_cannot_map', ['unmappable_missing_obx14']],
      ['other', ['new_one']],
    ]);
  });
});

describe("plausibilityNote: the mapper's range is information, not a limit", () => {
  it.each([
    ['systolic', '50', null],
    ['systolic', '300', null],
    ['diastolic', '20', null],
    ['diastolic', '200', null],
    ['systolic', 'abc', null],
  ] as const)('%s %s gets no note', (field, raw, note) => {
    expect(plausibilityNote(field, raw)).toBe(note);
  });

  it('notes a value the simulator sends but the mapper will reject', () => {
    expect(plausibilityNote('systolic', '301')).toBe(
      'Outside the mapper\'s plausible systolic range (50 to 300): it will be sent and stored, then rejected by the mapper with "out-of-range".',
    );
    expect(plausibilityNote('diastolic', '19')).toContain('(20 to 200)');
    expect(plausibilityNote('systolic', '49')).not.toBeNull();
  });
});
