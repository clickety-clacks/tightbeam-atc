/* Pure viewer rules shared with the browser and the package-free test suite. */
export function canInspectNode({type, layerVisible}) {
  return Boolean(type) && layerVisible === true;
}
