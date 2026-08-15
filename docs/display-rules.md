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

So the effective scope is `focus ∩ filter` when both are set. A background click
clears the focus only: an explicit search needs an explicit release, and a
stray click must not destroy a result set that was deliberately built. The
search chrome always describes the search, never the focus within it.

A consequence worth stating: a neighbourhood click frames the clicked node's
neighbourhood, not the filtered set. Framing the filter there was a bug.

## 2. `f` / space is a selection lens

While the fit owns the view, the selection *is* what is shown.

    12 search results lit, 4 brushed
    press f   ->  camera on the 4, only those 4 lit, the rest ghosted
    press f   ->  camera exactly where it was, the 12 lit again

The second press restores both the camera and the lighting. This makes "fly to
what I brushed" visually true even when the base view is filtered — previously
the camera could sit on nodes that were themselves ghosted out.

The return is exact for camera pose and orbit. It is not pixel-identical across
a viewport resize, and should not claim to be.

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
