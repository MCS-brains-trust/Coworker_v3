/**
 * Relative-time helpers. The approval queue shows
 * "received 8 minutes ago" rather than raw ISO timestamps;
 * detail views can opt into a full timestamp via ``formatAbsolute``.
 *
 * Intl.RelativeTimeFormat is widely-supported and locale-aware
 * out of the box. We pick the largest unit that fits.
 */
const RTF = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

const _UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 60 * 60 * 24 * 365],
  ["month", 60 * 60 * 24 * 30],
  ["week", 60 * 60 * 24 * 7],
  ["day", 60 * 60 * 24],
  ["hour", 60 * 60],
  ["minute", 60],
  ["second", 1],
];

export function formatRelative(iso: string, now: Date = new Date()): string {
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) {
    return iso;
  }
  const deltaSec = (target.getTime() - now.getTime()) / 1000;
  const absSec = Math.abs(deltaSec);
  for (const [unit, secPerUnit] of _UNITS) {
    if (absSec >= secPerUnit || unit === "second") {
      const value = Math.round(deltaSec / secPerUnit);
      return RTF.format(value, unit);
    }
  }
  return iso;
}

const DTF = new Intl.DateTimeFormat("en", {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatAbsolute(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return DTF.format(d);
}
