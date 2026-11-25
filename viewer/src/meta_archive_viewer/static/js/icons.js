/** Minimalist 1.5px-stroke inline SVG icon factory (zero innerHTML, zero external dependencies). */

const SVG_NS = ["http:", "", "www.w3.org", "2000", "svg"].join("/");

const ICON_SHAPES = {
  sun: [
    ["circle", { cx: "12", cy: "12", r: "4" }],
    ["path", { d: "M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" }],
  ],
  moon: [
    ["path", { d: "M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z" }],
  ],
  search: [
    ["circle", { cx: "11", cy: "11", r: "7" }],
    ["path", { d: "M20 20l-3.5-3.5" }],
  ],
  close: [
    ["path", { d: "M18 6L6 18M6 6l12 12" }],
  ],
  chevronLeft: [
    ["path", { d: "M15 18l-6-6 6-6" }],
  ],
  chevronRight: [
    ["path", { d: "M9 18l6-6-6-6" }],
  ],
  chevronUp: [
    ["path", { d: "M18 15l-6-6-6 6" }],
  ],
  chevronDown: [
    ["path", { d: "M6 9l6 6 6-6" }],
  ],
  heart: [
    ["path", { d: "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78L12 21.23l8.84-8.84a5.5 5.5 0 0 0 0-7.78z" }],
  ],
  heartFilled: [
    ["path", { d: "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78L12 21.23l8.84-8.84a5.5 5.5 0 0 0 0-7.78z", fill: "currentColor", stroke: "none" }],
  ],
  comment: [
    ["path", { d: "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" }],
  ],
  grid: [
    ["rect", { x: "3", y: "3", width: "7", height: "7", rx: "1" }],
    ["rect", { x: "14", y: "3", width: "7", height: "7", rx: "1" }],
    ["rect", { x: "14", y: "14", width: "7", height: "7", rx: "1" }],
    ["rect", { x: "3", y: "14", width: "7", height: "7", rx: "1" }],
  ],
  list: [
    ["path", { d: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" }],
  ],
  image: [
    ["rect", { x: "3", y: "4", width: "18", height: "16", rx: "2" }],
    ["circle", { cx: "8.5", cy: "9.5", r: "1.5" }],
    ["path", { d: "M21 16l-5-5L5 20" }],
  ],
  play: [
    ["polygon", { points: "7 5 19 12 7 19 7 5", fill: "currentColor", stroke: "none" }],
  ],
  layers: [
    ["rect", { x: "6", y: "6", width: "13", height: "13", rx: "2" }],
    ["path", { d: "M4 15V5a1 1 0 0 1 1-1h10" }],
  ],
  pin: [
    ["path", { d: "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" }],
    ["circle", { cx: "12", cy: "10", r: "3" }],
  ],
  folder: [
    ["path", { d: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" }],
  ],
  users: [
    ["path", { d: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" }],
    ["circle", { cx: "9", cy: "7", r: "4" }],
    ["path", { d: "M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" }],
  ],
  tag: [
    ["path", { d: "M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" }],
    ["circle", { cx: "7", cy: "7", r: "1.2", fill: "currentColor", stroke: "none" }],
  ],
  link: [
    ["path", { d: "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" }],
    ["path", { d: "M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" }],
  ],
  file: [
    ["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" }],
    ["polyline", { points: "14 2 14 8 20 8" }],
  ],
  phone: [
    ["path", { d: "M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.362 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.338 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" }],
  ],
  phoneMissed: [
    ["path", { d: "M23 1l-6 6M17 1l6 6" }],
    ["path", { d: "M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.362 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.338 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" }],
  ],
  archive: [
    ["polyline", { points: "21 8 21 21 3 21 3 8" }],
    ["rect", { x: "1", y: "3", width: "22", height: "5", rx: "1" }],
    ["line", { x1: "10", y1: "12", x2: "14", y2: "12" }],
  ],
  external: [
    ["path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }],
    ["polyline", { points: "15 3 21 3 21 9" }],
    ["line", { x1: "10", y1: "14", x2: "21", y2: "3" }],
  ],
  sparkles: [
    ["path", { d: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" }],
    ["path", { d: "M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z" }],
  ],
  calendar: [
    ["rect", { x: "3", y: "4", width: "18", height: "18", rx: "2" }],
    ["line", { x1: "16", y1: "2", x2: "16", y2: "6" }],
    ["line", { x1: "8", y1: "2", x2: "8", y2: "6" }],
    ["line", { x1: "3", y1: "10", x2: "21", y2: "10" }],
  ],
  shuffle: [
    ["polyline", { points: "16 3 21 3 21 8" }],
    ["line", { x1: "4", y1: "20", x2: "21", y2: "3" }],
    ["polyline", { points: "21 16 21 21 16 21" }],
    ["line", { x1: "15", y1: "15", x2: "21", y2: "21" }],
    ["line", { x1: "4", y1: "4", x2: "9", y2: "9" }],
  ],
  star: [
    ["polygon", { points: "12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" }],
  ],
  starFilled: [
    ["polygon", { points: "12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2", fill: "currentColor", stroke: "none" }],
  ],
  book: [
    ["path", { d: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20" }],
    ["path", { d: "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" }],
  ],
};

export function icon(name, size = 15, className = "ui-icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", className);

  const shapes = ICON_SHAPES[name] || [];
  for (const [tag, attrs] of shapes) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      node.setAttribute(k, v);
    }
    svg.appendChild(node);
  }
  return svg;
}
