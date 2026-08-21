# Display rules

The combinations below were undefined: the code did a bit of each answer and
the result depended on which feature had last touched the camera. These are the
decisions, made deliberately. `docs/state-review.md` is the analysis behind
them.

Throughout, **ghosted** means faint and still there — the org stays visible as
context. Nothing is ever hidden or removed from the scene.

## 1. A search stays in force under a click

Search establishes the working set; clicking inspects one of its results.

    type "watchdog"    ->  12 lit, everything else ghosted
    click one of them  ->  that one lit, everything else ghosted
                           the box still reads "12 shown"
    click background   ->  back to the 12
    empty the box      ->  back to everything

These are **layers on a stack**, not sets to intersect:

    filter   a search, or an agent's id list        (outermost)
    lens     f / space, showing the selection
    focus    a clicked node, then its neighbourhood (innermost)

**The innermost layer renders.** Enter a mode and that mode is what you see —
focus a node and only that node is lit, open its neighbourhood and the whole
neighbourhood is lit, including parts the search underneath would not have
matched. The layers below are remembered, not combined, so leaving one puts
back exactly what was true before it, camera included.

The same stack decides what you can reach: while a focus is up, hover, click
and the brush are limited to what it lights. A ghosted node is out of play —
clicking one does nothing, and it does not block a lit node behind it.

A background click pops the innermost layer. An explicit search needs an
explicit release, so it is never popped by a click; only the × or emptying the
box releases it. The search chrome always reports the search itself — focus one
of twelve results and it still reads "12 shown".

A consequence worth stating: a neighbourhood click frames the clicked node's
neighbourhood, not the filtered set. Framing the filter there was a bug.

## 2. `f` / space fits, it does not isolate

Two separate things:

- **Isolate** decides what is lit. A search does it. A focus does it.
- **Fit** decides only where the camera is. `f` does it, and ghosts nothing.

So `f` frames what you have collected and leaves the org fully lit. The fit is
live: the current selection defines the viewport, so every add and remove
re-frames it. With nothing selected `f` does nothing at all.

    4 brushed        ->  press f: camera on the 4, nothing ghosted
    brush 4 more     ->  camera re-frames on all 8
    click a node     ->  focus isolates it; the fit waits underneath
    click background ->  camera back on the 8, nothing ghosted
    press f          ->  focus dropped, camera back where it started

A background click pops one layer at a time: focus, then the fit, then the
search. Dismissing a search does not lose it — see below.

## 3. A human's stroke outranks an agent's command

A focus or filter arriving from the control API while a brush drag is in
progress is held until the pointer lifts, then applied in command order.

    drag starts          ->  scope A
      agent sends focus  ->  held
    pointer lifts        ->  the stroke is committed against scope A
                         ->  then the agent's focus applies

Applying mid-stroke changed which nodes the brush could reach halfway through,
so a drag collected from two different sets, and it started a camera flight
while brush mode claims the camera is frozen.

## 4. A search that matches nothing shows nothing

    type "zzzzz"  ->  nothing lit, the whole org ghosted, box reads "no match"

The search took effect and found nothing; the view says so. It must not fall
back to showing everything, which would read as "search off", nor keep lighting
the previous match, which would leave the screen disagreeing with the box. The
camera stays where it is — there is nothing to frame.

## 5. A dismissed search is kept

Every search — typed or applied by an agent — is filed as a card under the
status strip, newest on top. Clicking one runs it again and puts its terms back
in the box, where they can be edited rather than retyped. The × forgets it.

A saved card starts a new view from that card alone. Opening one clears node,
neighbourhood, and Arrow Focus, drops the fit and its camera return, and stops
any flight at the pose currently on screen. Selection stays selected. A card
with matches frames them; a card with no matches leaves the camera where it is.
An explicitly saved empty ID set stays empty even when the card also has terms.

That is what makes the third background click cheap: clearing a search is no
longer destructive, so a click can do it. Identical searches are refreshed
rather than piled up, so a patrol re-running the same query leaves one card and
not sixty.
