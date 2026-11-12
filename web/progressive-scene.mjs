/* Advance the existing scene builders between rendered frames. The iterator
   owns the complete model; a batch limit never changes its membership. */
export function createSceneLoad(steps, finish, {now = () => performance.now(),
  budgetMs = 6, batchSize = 32} = {}) {
  let pending = true;
  let held = false;
  return {
    get pending() { return pending; },
    get held() { return held; },
    hold(value) { held = Boolean(value); },
    cancel() { pending = false; steps.return?.(); },
    advance() {
      if (!pending || held) return false;
      const start = now();
      for (let count = 0; count < batchSize; count++) {
        const next = steps.next();
        if (next.done) {
          pending = false;
          finish();
          break;
        }
        if (now() - start >= budgetMs) break;
      }
      return true;
    },
  };
}
