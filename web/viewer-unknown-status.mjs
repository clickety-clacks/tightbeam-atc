export function unknownItemFacts({stageKnown, holdersKnown, landed, ring}) {
  const facts = [];
  if (!stageKnown) facts.push('stage is unknown');
  if (!holdersKnown) facts.push('holder information is unknown');
  if (landed && ring === 'unknown') facts.push('merge status is not confirmed');
  return facts;
}

export function accessibleUnknownDetails(facts) {
  return facts.length ? 'Unknown: ' + facts.join('; ') : '';
}
