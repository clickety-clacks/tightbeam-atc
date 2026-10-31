const linkedPrograms = new WeakSet();

export function areProgramsValid(gl, programs = []) {
  if (gl.isContextLost()) return false;
  for (const program of programs) {
    if (program.program === undefined || !gl.isProgram(program.program) ||
        program.diagnostics?.runnable === false) return false;
    if (!linkedPrograms.has(program)) {
      if (!gl.getProgramParameter(program.program, gl.LINK_STATUS)) return false;
      linkedPrograms.add(program);
    }
  }
  return true;
}

export function scheduleVisualChange(callback, delay, invalidate,
                                     setTimer = (fn, ms) => globalThis.setTimeout(fn, ms)) {
  if (typeof callback !== 'function') throw new TypeError('callback must be a function');
  if (typeof invalidate !== 'function') throw new TypeError('invalidate must be a function');
  if (typeof setTimer !== 'function') throw new TypeError('setTimer must be a function');
  return setTimer(() => {
    try { callback(); }
    finally { invalidate(); }
  }, delay);
}

export function renderInterval({cameraMoving = false, controlsChanged = false,
                                pulses = false, messages = false} = {}) {
  return cameraMoving || controlsChanged || pulses || messages ? 1000 / 60 : 0;
}

export function createRenderLoop({
  canvas,
  document: documentTarget = globalThis.document,
  draw,
  requestFrame = globalThis.requestAnimationFrame?.bind(globalThis),
  cancelFrame = globalThis.cancelAnimationFrame?.bind(globalThis),
  setTimer = globalThis.setTimeout?.bind(globalThis),
  clearTimer = globalThis.clearTimeout?.bind(globalThis),
  now = () => globalThis.performance?.now?.() ?? Date.now(),
  maxAnimationFps = 30,
  isContextValid = () => true,
  onContextLost = () => {},
  onContextRestored = () => {},
} = {}) {
  if (typeof draw !== 'function') throw new TypeError('draw must be a function');
  if (typeof requestFrame !== 'function' || typeof cancelFrame !== 'function')
    throw new TypeError('animation frame functions are required');

  let frameId = null;
  let timerId = null;
  let dirty = false;
  let animationInterval = 0;
  let lastDraw = -Infinity;
  let pageVisible = !documentTarget?.hidden;
  let contextLost = false;
  let manuallyPaused = false;
  let disposed = false;

  function canDraw() {
    return !disposed && pageVisible && !contextLost && !manuallyPaused && isContextValid();
  }

  function cancelScheduled() {
    if (frameId !== null) {
      cancelFrame(frameId);
      frameId = null;
    }
    if (timerId !== null) {
      clearTimer?.(timerId);
      timerId = null;
    }
  }

  function queueFrame() {
    if (frameId !== null || !canDraw()) return;
    frameId = requestFrame(frame);
  }

  function schedule(urgent = false) {
    if (!canDraw() || frameId !== null || timerId !== null || (!dirty && !animationInterval)) return;
    const wait = urgent ? 0 : Math.max(0, animationInterval - (now() - lastDraw));
    if (wait > 0 && typeof setTimer === 'function') {
      timerId = setTimer(() => {
        timerId = null;
        queueFrame();
      }, wait);
    } else {
      queueFrame();
    }
  }

  function invalidate() {
    dirty = true;
    if (timerId !== null) {
      clearTimer?.(timerId);
      timerId = null;
    }
    schedule(true);
  }

  function frame(timestamp) {
    frameId = null;
    if (!canDraw()) return;
    if (!dirty && !animationInterval) return;

    dirty = false;
    const nextAnimation = draw(timestamp);
    lastDraw = now();
    if (typeof nextAnimation === 'number' && Number.isFinite(nextAnimation)) {
      animationInterval = Math.max(0, nextAnimation);
    } else {
      animationInterval = nextAnimation ? 1000 / maxAnimationFps : 0;
    }
    schedule();
  }

  function handleVisibilityChange() {
    pageVisible = !documentTarget.hidden;
    if (pageVisible) invalidate();
    else cancelScheduled();
  }

  function handleContextLost(event) {
    event?.preventDefault?.();
    contextLost = true;
    cancelScheduled();
    onContextLost(event);
  }

  function handleContextRestored(event) {
    contextLost = false;
    onContextRestored(event);
    invalidate();
  }

  documentTarget?.addEventListener('visibilitychange', handleVisibilityChange);
  canvas?.addEventListener('webglcontextlost', handleContextLost, false);
  canvas?.addEventListener('webglcontextrestored', handleContextRestored, false);

  invalidate();

  return {
    invalidate,
    pause() {
      manuallyPaused = true;
      cancelScheduled();
    },
    resume() {
      manuallyPaused = false;
      invalidate();
    },
    get contextLost() { return contextLost; },
    get scheduled() { return frameId !== null || timerId !== null; },
    destroy() {
      if (disposed) return;
      disposed = true;
      cancelScheduled();
      documentTarget?.removeEventListener('visibilitychange', handleVisibilityChange);
      canvas?.removeEventListener('webglcontextlost', handleContextLost, false);
      canvas?.removeEventListener('webglcontextrestored', handleContextRestored, false);
    },
  };
}
