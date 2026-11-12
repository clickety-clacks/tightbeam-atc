/* View membership and age are independent of stage and merge-ring styling. */
export const DAY_MS = 86400000;
export const DEFAULT_FINISHED_DAYS = 1;
export const MAX_FINISHED_DAYS = 30;
export const RECENT_ORPHAN_DAYS = 3;
export const EDGE_ORPHAN_DAYS = 30;

const time = value => typeof value === 'number' && Number.isFinite(value) && value > 0
  ? value : null;

// These are normalized evidence records in the ATC served model, not a new
// upstream API. Current upstream records supply none. Neither prose nor a
// user-identity attribution is proof of a direct operator action.
export function failureAcceptance(item) {
  const evidence = item.failureAcceptance;
  const records = (Array.isArray(evidence?.records) ? evidence.records : []).filter(row =>
    row.itemId === item.id && typeof row.id === 'string' && row.id.length > 0
    && ['operator-action', 'operator-decision'].includes(row.source)
    && typeof row.accepted === 'boolean');
  const yes = records.filter(row => row.accepted);
  const no = records.filter(row => !row.accepted);
  const conflict = yes.length > 0 && no.length > 0;
  const accepted = !conflict && yes.length > 0 && evidence?.complete === true;
  const dates = yes.map(row => time(row.at));
  return {accepted, conflict, records,
    at: accepted && dates.every(at => at !== null) ? Math.min(...dates) : null};
}

export function unacceptedFailure(item) {
  return item?.state === 'failed' && !failureAcceptance(item).accepted;
}

export function failureDescription(item) {
  if (item?.state !== 'failed') return '';
  const result = failureAcceptance(item);
  if (result.conflict) return 'Acceptance evidence conflicts; treated as unaccepted pending product-owner ruling.';
  if (result.accepted) return 'Failure accepted: ' + result.records.map(row => row.source + ' ' + row.id).join(', ');
  return 'Failure unaccepted: no conclusive acceptance evidence is available.';
}

export function itemWindow(item) {
  if (item.state === 'failed') {
    const acceptance = failureAcceptance(item);
    if (!acceptance.accepted) return null;
    const failedAt = time(item.failedAt);
    const exact = failedAt !== null && acceptance.at !== null;
    return {kind:'finished', at:exact ? Math.max(failedAt, acceptance.at)
      : time(item.latestActivityAt), approximate:!exact};
  }
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
