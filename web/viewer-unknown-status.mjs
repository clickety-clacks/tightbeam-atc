export function unknownItemType(item) {
  return item?.code !== true && item?.code !== false;
}

function knownMerge(item) {
  return typeof item?.merged === 'boolean'
    && item?.evidence?.merged?.status === 'known'
    && item.evidence.merged.complete !== false;
}

function knownReadyToMerge(item) {
  return typeof item?.readyToMerge === 'boolean'
    && item?.evidence?.attests?.status === 'known'
    && item.evidence.attests.complete !== false;
}

function knownStalled(item) {
  return typeof item?.stalled === 'boolean'
    && item?.evidence?.stalled?.status === 'known'
    && item.evidence.stalled.complete !== false;
}

export function unknownItemFacts({stageKnown, holdersKnown, item, landed, ring}) {
  const facts = [];
  if (!stageKnown) facts.push('stage is unknown');
  if (!holdersKnown) facts.push('holder information is unknown');
  if (item && unknownItemType(item)) facts.push('code or non-code type is unknown');
  if (landed && (item ? !knownMerge(item) : ring === 'unknown')) {
    facts.push('merge status is not confirmed');
  }
  if (item && !knownReadyToMerge(item)) facts.push('ready-to-merge status is unknown');
  if (item && !knownStalled(item)) facts.push('stalled status is unknown');
  return facts;
}

export function accessibleUnknownDetails(facts) {
  return facts.length ? 'Unknown: ' + facts.join('; ') : '';
}
