/**
 * The figure in the centre of the page: original, simple line art drawn here as inline SVG (no
 * image file, icon set or font). Muted strokes only, no fills and no organs (brief, "Single
 * page"). A cuff is marked on the upper arm on the left (the episodic, blood-pressure side) and a
 * watch on the wrist on the right (the continuous, heart-rate side), and an arrow leaves the body
 * from each toward its panel. Colours come from CSS custom properties (styles/tokens.css), so the
 * figure follows light and dark schemes.
 */
import { svg, uniqueId } from '../ui/dom';

export function silhouette(): SVGSVGElement {
  const titleId = uniqueId('figure-title');
  const descId = uniqueId('figure-desc');
  const title = svg('title', { id: titleId });
  title.textContent = 'Line drawing of a person wearing a blood-pressure cuff and a watch';
  const desc = svg('desc', { id: descId });
  desc.textContent =
    'A cuff on the upper arm sends readings to the left side of the page; a watch on the wrist ' +
    'sends heart rate to the right side.';

  const body = svg(
    'g',
    { class: 'figure__body' },
    svg('circle', { cx: 120, cy: 40, r: 21 }), // head
    svg('path', { d: 'M120 61 V72' }), // neck
    // shoulders, torso and hips as one closed outline
    svg('path', { d: 'M86 84 Q120 70 154 84 L148 180 Q120 188 92 180 Z' }),
    svg('path', { d: 'M86 86 L72 140 L65 192' }), // arm on the left of the picture
    svg('path', { d: 'M154 86 L168 140 L175 192' }), // arm on the right
    svg('path', { d: 'M104 184 L100 298 M136 184 L140 298' }), // legs
    svg('path', { d: 'M100 298 H88 M140 298 H152' }), // feet
  );

  // Cuff: a band around the upper arm, rotated to follow it, with its tube and an arrow out.
  const cuff = svg(
    'g',
    { class: 'figure__cuff' },
    svg('rect', { x: 70, y: 104, width: 18, height: 18, rx: 3, transform: 'rotate(15 79 113)' }),
    svg('path', { d: 'M70 113 H14' }),
    svg('path', { d: 'M24 105 L14 113 L24 121' }),
  );

  // Watch: a small face on the wrist, rotated with the forearm, and an arrow out to the right.
  const watch = svg(
    'g',
    { class: 'figure__watch' },
    svg('rect', {
      x: 167,
      y: 177,
      width: 14,
      height: 11,
      rx: 2.5,
      transform: 'rotate(-8 174 182)',
    }),
    svg('path', { d: 'M183 182 H226' }),
    svg('path', { d: 'M216 174 L226 182 L216 190' }),
  );

  return svg(
    'svg',
    {
      class: 'figure__svg',
      viewBox: '0 0 240 310',
      role: 'img',
      'aria-labelledby': titleId,
      'aria-describedby': descId,
      focusable: 'false',
    },
    title,
    desc,
    body,
    cuff,
    watch,
  );
}
