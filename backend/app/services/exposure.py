"""What counts as having seen a piece: exposure, versioned.

Four things are kept apart:
* returned: the pieces a feed request returned (FeedRequest.items);
* rendered: a tile put on the page. Not an exposure, and not recorded: a grid renders pieces far below the fold;
* exposed: at least VISIBLE_FRACTION of the tile (or of the screen, for a tile taller than it) in view, in a tab you
  are looking at, for DWELL_MS without interruption, or a decision you made about it while it was on screen. One
  continuous stretch on screen is one exposure, however many feed answers arrive meanwhile; scrolling it away,
  switching tab or leaving the page ends the stretch (a gap shorter than GRACE_MS does not: a re-ranked grid reports
  moved tiles out of view for a frame or two);
* rated: an explicit decision (feedback).

Each exposure is recorded once, by the id the browser gives it (a retry after a failed delivery is a duplicate).
DEFINITION names this rule and is stored with every record. Records made before it ("rendered" tiles: definition
None) are kept but never used as the denominator of an exposure rate: rates use records of the current definition
only, and are reported as unavailable when there are none (eval/selective.py, served feeds).
"""

from __future__ import annotations

DEFINITION = "exposure-v2"
DWELL_MS = 1000
VISIBLE_FRACTION = 0.5
GRACE_MS = 250
