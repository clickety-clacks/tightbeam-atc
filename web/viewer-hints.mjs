import { ringMeaning } from './viewer-semantics.mjs';
import { OPEN_WORK_STAGE_QUALIFIER } from './work-item-tiers.mjs';

const text = value => typeof value === 'string' && value.trim() ? value : null;

function hintFact(entity, fact, value, render) {
  const evidence = entity?.evidence?.[fact];
  if (evidence?.status !== 'known' || evidence.complete !== true) return null;
  const label = render(value);
  if (typeof label !== 'string' || !label.trim()) return null;

  const qualifiers = [];
  if (evidence.freshness === 'stale') qualifiers.push('stale');
  if (evidence.freshness === 'unconfirmed' || Number.isFinite(evidence.unconfirmedSince))
    qualifiers.push('unconfirmed');
  if (evidence.freshness === 'rebuilding') qualifiers.push('rebuilding');
  if (!['live', 'stale', 'unconfirmed', 'rebuilding'].includes(evidence.freshness))
    qualifiers.push('freshness unknown');
  return {value: label, qualifiers};
}

function hintFactText(fact, additionalQualifier = null) {
  if (!fact) return additionalQualifier ? `unknown (${additionalQualifier})` : 'unknown';
  const qualifiers = fact.qualifiers.join(', ')
    + (additionalQualifier ? `${fact.qualifiers.length ? '; ' : ''}${additionalQualifier}` : '');
  return qualifiers
    ? `${fact.value} (${qualifiers})`
    : fact.value;
}

const TURN_LABELS = {run: 'running', wait: 'queued', idle: 'idle'};
const RING_LABELS = {
  black: 'merged',
  green: 'ready to merge',
  'light-gray': 'non-code',
  yellow: 'not confirmed',
  none: 'not at merge stage',
  unknown: null,
};

export function agentHintSummary(agent) {
  const name = hintFact(agent, 'identity', agent?.name, text);
  const identity = hintFact(agent, 'identity',
    agent?.retired === true ? 'retired' : agent?.retired === false ? 'active' : null,
    text);
  const turn = hintFact(agent, 'turn', agent?.turn,
    value => TURN_LABELS[value] || null);

  return {
    identity: name ? hintFactText(name) : text(agent?.id) || 'Agent identity unknown',
    summary: `state: ${hintFactText(identity)} · turn: ${hintFactText(turn)}`,
  };
}

export function itemHintSummary(item) {
  const title = hintFact(item, 'identity', item?.title, text);
  const state = hintFact(item, 'identity', item?.state, text);
  const stage = hintFact(item, 'stage', item?.stage,
    value => Number.isInteger(value) && value >= 0 && value <= 6 ? String(value) : null);
  const openAssignments = hintFact(item, 'assignments', item?.assignments?.open,
    value => Number.isSafeInteger(value) && value >= 0 ? String(value) : null);
  const ring = ringMeaning(item?.ring, item?.evidence?.ring?.status,
    item?.evidence?.ring?.complete);
  const ringFact = hintFact(item, 'ring', ring, value => RING_LABELS[value] || null);
  const merge = ring === 'unknown' ? 'not confirmed' : hintFactText(ringFact);
  const stageText = hintFactText(stage,
    item?.state === 'open' ? OPEN_WORK_STAGE_QUALIFIER : null);

  return {
    identity: text(item?.short) || text(item?.id) || 'Work item identity unknown',
    title: title ? hintFactText(title) : 'Title unknown',
    summary: `state: ${hintFactText(state)} · stage: ${stageText}`
      + ` · open assignments: ${hintFactText(openAssignments)} · merge status: ${merge}`,
  };
}
