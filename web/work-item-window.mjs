/* View membership and age are independent of stage and merge-ring styling.
   Failed placement remains unchanged until its product decision is settled. */
export const DAY_MS = 86400000;
export const DEFAULT_FINISHED_DAYS = 1;
export const MAX_FINISHED_DAYS = 30;
export const RECENT_ORPHAN_DAYS = 3;
export const EDGE_ORPHAN_DAYS = 30;

const time = value => typeof value === 'number' && Number.isFinite(value) && value > 0
  ? value : null;

export function itemWindow(item) {
  if (item.state === 'failed') return null;
  if (item.state === 'closed' || item.state === 'iceboxed') {
    return {kind: 'finished', at: time(item.finishedAt),
      approximate: item.finishedAtApproximate === true};
  }
  if (item.merged === true) {
    const observed = (Array.isArray(item.landings) ? item.landings : [])
      .map(row => time(row.observedAt)).filter(at => at !== null);
    return {kind: 'landed', at: observed.length ? Math.min(...observed)
      : time(item.landingAt), approximate: false};
  }
  return null;
}

export function itemInWindow(item, days = DEFAULT_FINISHED_DAYS, now = Date.now()) {
  const window = itemWindow(item);
  return !window || (days > 0 && window.at !== null && now - window.at < days * DAY_MS);
}

export function orphanRadius(item, inner, outer, now = Date.now()) {
  const latest = time(item.latestActivityAt);
  if (latest === null) return outer;
  const age = Math.max(0, now - latest) / (EDGE_ORPHAN_DAYS * DAY_MS);
  // Linear age distance up to thirty days, then a fixed outer edge.
  return inner + (outer - inner) * Math.min(1, age);
}

export function windowDescription(item) {
  const window = itemWindow(item);
  if (!window) return '';
  return `${window.kind} time: ${window.at === null ? 'unknown'
    : new Date(window.at).toISOString()}${window.approximate ? ' (approximate, latest recorded activity)' : ''}`;
}
