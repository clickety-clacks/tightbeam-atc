export function createContextGeneration() {
  let generation = 0;

  return {
    advance() { generation += 1; },
    guard(callback, onStale = () => {}) {
      if (typeof callback !== 'function' || typeof onStale !== 'function')
        throw new TypeError('context callbacks must be functions');

      const capturedGeneration = generation;
      let active = true;
      return event => {
        if (!active) return;
        if (capturedGeneration !== generation) {
          active = false;
          onStale(event);
          return;
        }
        return callback(event);
      };
    },
  };
}
