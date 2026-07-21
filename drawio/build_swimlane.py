#!/usr/bin/env python3
"""Hand-built swimlane lifecycle diagram -> draw.io mxGraph XML.

Design principles for a clean, non-spaghetti result:
  * Row-aligned grid: nodes that connect horizontally share a Y row, so the
    connecting edge is a straight line (no diagonals).
  * Four actor lanes (HUMAN / FIX / STATE / REVIEW) as background bands;
    steward spans the top.
  * Loops route through dedicated vertical channels between lanes via explicit
    waypoints, never through a node.
  * Explicit exit/entry perimeter points per edge so arrows dock cleanly.
Vertex-first ordering; infinite canvas (page=0)."""
from html import escape

ROUND = "rounded=1;whiteSpace=wrap;html=1;"
PILL  = "rounded=1;arcSize=50;whiteSpace=wrap;html=1;"
RECT  = "rounded=0;whiteSpace=wrap;html=1;"

def role(fill, stroke, font):
    return f"fillColor={fill};strokeColor={stroke};fontColor={font};"

HUMAN  = role("#fef3c7", "#d97706", "#92400e")
FIX    = role("#dbeafe", "#2563eb", "#1e3a8a")
REVIEW = role("#d1fae5", "#059669", "#065f46")
STATE  = role("#e2e8f0", "#334155", "#1e293b")
GOOD   = role("#dcfce7", "#16a34a", "#15803d")
STEW   = role("#f5f3ff", "#7c3aed", "#5b21b6")
DARK   = role("#334155", "#1e293b", "#ffffff")
DEC    = "rhombus;whiteSpace=wrap;html=1;" + role("#fff7ed", "#ea580c", "#9a3412")
GATE   = "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;" + role("#fee2e2", "#dc2626", "#b91c1c")

# ---- lane geometry ----
LANE_Y, LANE_H = 178, 940
# F–S gutter widened to ~58px (laneF ends 560, laneS starts 618) so the two blue
# return channels have clear air between fix_ci's edge and the channel.
lanes = [
    ("laneH",  40, LANE_Y, 210, LANE_H, "#fffbeb", "#fde68a", "HUMAN", "#b45309"),
    ("laneF", 268, LANE_Y, 292, LANE_H, "#eff6ff", "#bfdbfe", "FIX AGENT · fix-bot", "#1d4ed8"),
    ("laneS", 618, LANE_Y, 252, LANE_H, "#f8fafc", "#e2e8f0", "GitHub state", "#334155"),
    ("laneR", 888, LANE_Y, 322, LANE_H, "#ecfdf5", "#a7f3d0", "REVIEW AGENT · review-bot", "#047857"),
]
# lane centre x
cxH, cxF, cxS, cxR = 145, 414, 744, 1049
# blue return-loop channels, sitting in the widened F–S gutter (560..618):
CH_IN, CH_OUT = 600, 582

# ---- row anchors: Y of each row's CENTER LINE ----
# Nodes are placed by centre (see node()), so any two nodes on the same row are
# guaranteed to share a vertical centre -> their horizontal connector is exactly
# horizontal (no slope). Horizontal partners MUST be put on the same row.
# R1 is pushed down a little so the first STATE node (ai-ready) clears the lane
# title. GATE has its own raised cy (the STATE column is empty between ai-ready and
# the gate, so the gate floats up to sit just under resolve's row — this kills the
# long downward stub on the resolve->gate edge). Lower rows are tightened upward.
R1, R2, R3, R4, R5, R6, R7, R8, R9 = 252, 352, 452, 520, 626, 742, 836, 922, 1004
GATE_CY = 432                 # gate floats up near resolve's row (R2=352)

GEO = {}  # nid -> (x, y, w, h); populated after `nodes` is built

def node(nid, cx, w, cy, h, base, rs, label):
    """Place by centre (cx, cy) so same-row nodes align for clean horizontals."""
    return (nid, cx - w // 2, cy - h // 2, w, h, base, rs, label)

def b(*lines):
    return "&lt;br&gt;".join(lines)

nodes = [
    ("steward", 40, 70, 1170, 88, ROUND, STEW,
     "&lt;b&gt;ARCHITECTURE STEWARD · steward-bot&lt;/b&gt;&lt;br&gt;owns INVARIANTS.md + ADRs · drift as signal-not-gate&lt;br&gt;attaches pre-work design context · mines recurring feedback into durable defaults"),

    # HUMAN column — rows chosen to pair with their horizontal partners:
    #   file_issue@R1 → ai_ready@R1   |  human_takeover@R7 ← needs_human@R7  |  prod@R9 ← ai_done@R9
    node("file_issue",    cxH, 168, R1, 56, ROUND, HUMAN, b("&lt;b&gt;① File issue&lt;/b&gt;","human writes","acceptance criteria")),
    node("human_replies", cxH, 168, R3, 52, ROUND, HUMAN, b("&lt;b&gt;Human replies&lt;/b&gt;","any teammate · async")),
    node("human_takeover",cxH, 168, R7, 54, ROUND, HUMAN, b("&lt;b&gt;Human takes over&lt;/b&gt;","reviews / decides")),
    node("prod",          cxH, 168, R9, 56, ROUND, HUMAN, b("&lt;b&gt;Merge dev → prod&lt;/b&gt;","human · out of scope")),

    # FIX column — resolve@R2 pairs with ai_blocked@R2; fix_ci@R5 pairs with ci_green@R5; address@R6 pairs with verdict@R6
    node("resolve", cxF, 210, R2, 86, ROUND, FIX, b("&lt;b&gt;resolve-issue.sh&lt;/b&gt;","claude -p --worktree","sets ai-working","&lt;i&gt;TDD → see Diagram 2&lt;/i&gt;")),
    node("fix_ci",  cxF, 200, R5, 64, ROUND, FIX, b("&lt;b&gt;fix-ci.sh&lt;/b&gt;","claude -p --resume","≤3 tries")),
    node("address", cxF, 200, R6, 64, ROUND, FIX, b("&lt;b&gt;address-review.sh&lt;/b&gt;","claude -p --resume","→ push")),

    # STATE spine (centre) — ci_green@R5 aligns with both fix_ci (left) and review_pr (right)
    node("ai_ready",   cxS, 116, R1, 44, PILL, STATE, "&lt;b&gt;ai-ready&lt;/b&gt;"),
    node("gate",       cxS, 138, GATE_CY, 96, GATE, "", b("&lt;b&gt;GATE — hooks&lt;/b&gt;","on gh pr create","branch·commit·PR")),
    node("ai_pr_open", cxS, 120, R4, 48, PILL, STATE, b("&lt;b&gt;ai-pr-open&lt;/b&gt;","PR → dev")),
    node("ci_green",   cxS, 132, R5, 92, DEC, "", "&lt;b&gt;CI green?&lt;/b&gt;"),
    node("approved",   cxS, 116, R7, 44, PILL, GOOD, "&lt;b&gt;approved&lt;/b&gt;"),
    node("merge",      cxS, 150, R8, 52, ROUND, DARK, b("&lt;b&gt;merge → dev&lt;/b&gt;","auto-merge or human")),
    node("ai_done",    cxS, 116, R9, 44, PILL, STATE, "&lt;b&gt;ai-done&lt;/b&gt;"),

    # ai-blocked: in the HUMAN lane, ON resolve's row R2 so the blocked edge is horizontal
    node("ai_blocked", cxH, 116, R2, 44, PILL, STATE, "&lt;b&gt;ai-blocked&lt;/b&gt;"),

    # REVIEW column — review_pr@R5 (← ci_green), verdict@R6 (← address), needs_human@R7 (→ human_takeover)
    node("review_pr",   cxR, 262, R5, 74, ROUND, REVIEW, b("&lt;b&gt;review-pr.sh / re-review-pr.sh&lt;/b&gt;","claude -p · diff (+ optional checkout)","different model recommended")),
    node("verdict",     cxR, 150, R6, 92, DEC, "", b("&lt;b&gt;verdict?&lt;/b&gt;","schema decision")),
    node("needs_human", cxR, 224, R7, 44, PILL, REVIEW, "&lt;b&gt;needs-human-review&lt;/b&gt;"),
]

# Build node-geometry lookup (id -> x,y,w,h) for perpendicular/overlap logic.
for _n in nodes:
    GEO[_n[0]] = (_n[1], _n[2], _n[3], _n[4])

# ---- edges ----
# tuple: id-prefix, src, tgt, label, color, dashed, exit(x,y), entry(x,y), [waypoints]
SOLID, DASH = "0", "1"
C_HAPPY, C_AMBER, C_BLUE, C_GREEN, C_PURPLE = "#1e293b", "#d97706", "#2563eb", "#059669", "#7c3aed"

E = []  # each: dict
def edge(s, t, lbl, col, dash, ex, ey, nx, ny, pts=None, ldx=0, ldy=0, lpos=0.0):
    # ldx/ldy nudge the label OFF the edge line (perpendicular) so a long label
    # doesn't blanket a whole segment and hide the edge path. Negative ldy = above.
    # lpos slides the label ALONG the edge (-1 source end .. 0 mid .. +1 target end);
    # use it to move a label onto a segment that runs through clear space instead of
    # the geometric midpoint (which may fall on a cramped gutter beside a node).
    E.append(dict(s=s, t=t, lbl=lbl, col=col, dash=dash, ex=ex, ey=ey, nx=nx, ny=ny,
                  pts=pts or [], ldx=ldx, ldy=ldy, lpos=lpos))

def face_point(nid, fx, fy):
    """Absolute (x,y) of the fractional perimeter point (fx,fy) on a node."""
    x, y, w, h = GEO[nid]
    return (x + fx * w, y + fy * h)

def face_normal(fx, fy):
    """Outward axis of a perimeter point: 'h' (left/right face) or 'v' (top/bottom)."""
    # left=x0, right=x1 -> horizontal normal; top=y0, bottom=y1 -> vertical normal.
    if fx in (0, 1) and fy not in (0, 1):
        return "h"
    if fy in (0, 1) and fx not in (0, 1):
        return "v"
    # corner / centre exit: pick the face that is exactly on an edge
    if fx in (0, 1):
        return "h"
    return "v"

def enforce_perpendicular(e):
    """Guarantee the FIRST segment leaves the source perpendicular to its face and
    the LAST segment enters the target perpendicular to its face, by snapping/adding
    a stub waypoint just off each face. Fixes:
      * arrowheads rendering rotated (last segment not axis-aligned to the face), and
      * edges that leave a node parallel to (sliding along) its edge.
    """
    sp = face_point(e["s"], e["ex"], e["ey"])
    tp = face_point(e["t"], e["nx"], e["ny"])
    sn = face_normal(e["ex"], e["ey"])
    tn = face_normal(e["nx"], e["ny"])
    pts = list(e["pts"])

    # source stub: first waypoint must lie along the outward normal from sp.
    if pts:
        first = pts[0]
        ok = (sn == "v" and abs(first[0] - sp[0]) < 0.5) or (sn == "h" and abs(first[1] - sp[1]) < 0.5)
        if not ok:
            pts.insert(0, (sp[0], first[1]) if sn == "v" else (first[0], sp[1]))
    # target stub: last waypoint must lie along the inward normal to tp.
    if pts:
        last = pts[-1]
        ok = (tn == "v" and abs(last[0] - tp[0]) < 0.5) or (tn == "h" and abs(last[1] - tp[1]) < 0.5)
        if not ok:
            pts.append((tp[0], last[1]) if tn == "v" else (last[0], tp[1]))
    e["pts"] = pts

# happy path (solid, bold, dark) — spine + clean horizontal hops
# Exit steward at the x-fraction directly above ai_ready's centre so the edge is a
# clean vertical (no jog). steward spans the canvas; fraction = (ai_ready_cx - sx)/sw.
_sx, _sw = 40, 1170
_aicx = GEO["ai_ready"][0] + GEO["ai_ready"][2] / 2
_sf = (_aicx - _sx) / _sw
# label lifted up (ldy=-12) so it rides ABOVE the segment and clears the lane title.
edge("steward", "ai_ready", "design context (pre-work)", C_PURPLE, DASH, _sf, 1, 0.5, 0, ldx=70, ldy=-12)
edge("file_issue", "ai_ready", "fix-watcher", C_HAPPY, SOLID, 1, 0.5, 0, 0.5)               # horizontal R1
# ai_ready (R1, STATE col) -> resolve (R2, FIX col, down-left). Exit ai_ready's
# BOTTOM, drop to resolve's row, then straight into resolve's RIGHT face. The long
# vertical drop gives the arrowhead a clean run; final segment is horizontal into
# the face. (Avoids the old short-stub-into-top look.)
edge("ai_ready", "resolve", "", C_HAPPY, SOLID, 0.5, 1, 1, 0.30, [(cxS, R2 - 26)])
# resolve (R2) -> gate (just below, GATE_CY): exit resolve RIGHT, run to the spine
# at resolve's row, drop straight into gate's TOP. Clean L, short drop.
edge("resolve", "gate", "", C_HAPPY, SOLID, 1, 0.70, 0.5, 0, [(cxS, R2 + 26)])
# short gap label -> offset right so it doesn't blanket the segment
edge("gate", "ai_pr_open", "PR created → dev", C_HAPPY, SOLID, 0.5, 1, 0.5, 0, ldx=64, ldy=0)
edge("ai_pr_open", "ci_green", "", C_HAPPY, SOLID, 0.5, 1, 0.5, 0)                          # vertical
edge("ci_green", "review_pr", "yes · pass", C_HAPPY, SOLID, 1, 0.5, 0, 0.5)                 # horizontal R5
edge("review_pr", "verdict", "", C_GREEN, SOLID, 0.5, 1, 0.5, 0)                            # vertical
# verdict has three outgoing edges; give each its own face/point so none overlap:
#   APPROVE -> approved: exit verdict LEFT-upper (0,0.30), drop down the spine into approved top
#   REQUEST_CHANGES -> address: exit verdict LEFT-mid (0,0.50) -> address RIGHT 0.5 (both @R6 => horizontal)
#   ESCALATE -> needs_human: exit verdict BOTTOM (0.5,1)
# Corner waypoint y MUST equal the exit-point y, else enforce_perpendicular inserts
# a stub that backtracks and the corner renders as a sharp notch (not rounded).
_appr_y = face_point("verdict", 0, 0.30)[1]
edge("verdict", "approved", "APPROVE", C_HAPPY, SOLID, 0, 0.30, 0.5, 0, [(cxS, _appr_y)])    # single clean rounded corner at the spine
edge("approved", "merge", "", C_HAPPY, SOLID, 0.5, 1, 0.5, 0)                               # vertical
edge("merge", "ai_done", "", C_HAPPY, SOLID, 0.5, 1, 0.5, 0)                                # vertical

# blocked loop (amber) — HUMAN gutter. resolve↔ai_blocked share R2 → perfectly horizontal.
edge("resolve", "ai_blocked", "blocked", C_AMBER, DASH, 0, 0.5, 1, 0.5)                     # horizontal R2
edge("ai_blocked", "human_replies", "", C_AMBER, DASH, 0.5, 1, 0.5, 0)                      # straight down (same column cxH)
# human_replies → resolve: exit RIGHT face, run right at R3, then straight UP into
# resolve's BOTTOM face (entry 0.18). The vertical final segment makes the arrowhead
# point cleanly UP into the box. Waypoint x == entry x so the up-run is plumb.
RES_BL = GEO["resolve"][0] + 0.18 * GEO["resolve"][2]   # bottom-entry x on resolve
edge("human_replies", "resolve", "resume-issue.sh", C_AMBER, DASH, 1, 0.5, 0.18, 1, [(RES_BL, R3)])

# Two blue return loops share the gutter between FIX and STATE lanes but use
# SEPARATE channels and SEPARATE entry heights on ai_pr_open's left edge.
# ai_pr_open spans y=[R4 .. R4+46]; entry at 0.30 -> y≈R4+14, at 0.70 -> y≈R4+32.
# Channels sit left of the STATE lane (laneS starts x=578): x=560 (inner) and x=540 (outer).
# Each edge: exit RIGHT edge of source -> straight to channel -> straight up -> straight into target. No overshoot.
# ai_pr_open is at row R4, height 46 → spans y∈[R4-23, R4+23]; entry 0.30→R4-9, 0.70→R4+9.
# fix_ci@R5 and address@R6 exit their RIGHT edge, run straight to a channel, straight up, straight in.
# fix_ci RIGHT face: ci_green ENTERS at y=0.5; the return to ai_pr_open EXITS at
# y=0.30 so the two don't share a point (issue: exit==entry overlap).
edge("ci_green", "fix_ci", "fail", C_BLUE, DASH, 0, 0.5, 1, 0.5)                            # enters fix_ci RIGHT @0.5
# Channel return loops. Compute the ai_pr_open entry-y from geometry so the final
# vertical channel segment lands EXACTLY on the entry point (no 1px notch). The
# channel waypoints share x with the entry; the second waypoint shares y with the
# entry -> a clean up-then-in L. Mid-segment is vertical -> offset label via ldx.
_aip = GEO["ai_pr_open"]                         # (x, y, w, h)
_aip_y30 = _aip[1] + 0.30 * _aip[3]
_aip_y70 = _aip[1] + 0.70 * _aip[3]
_fix_y30 = GEO["fix_ci"][1] + 0.30 * GEO["fix_ci"][3]
_adr_y30 = GEO["address"][1] + 0.30 * GEO["address"][3]
# fix_ci -> ai_pr_open: exit the TOP face, straight up, then right into ai_pr_open's
# left — a clean square L. The single corner waypoint shares x with the exit (plumb
# up) and y with the entry (level across). The label rides the horizontal top
# segment, which runs through open gutter, so it sits ON its own path (lifted above
# with ldy). Replaces the old right-face route whose label drifted far from the line.
_fix_topx = GEO["fix_ci"][0] + 0.5 * GEO["fix_ci"][2]
edge("fix_ci", "ai_pr_open", "push ⟳ CI · ≤3 → ai-blocked", C_BLUE, DASH, 0.5, 0, 0, 0.30,
     [(_fix_topx, _aip_y30)], ldy=-12)
edge("verdict", "address", "REQUEST_CHANGES → comment", C_GREEN, DASH, 0, 0.5, 1, 0.5)      # both @R6 → perfectly horizontal
# address -> ai_pr_open: route up the CLEAR LEFT side of the FIX lane (x=LEFT_CH,
# left of fix_ci) instead of the cramped centre channels — this decongests the
# gutter AND gives the label a long vertical segment in open space (it no longer
# lands on fix_ci). Exit address's LEFT face, up the clear column, over into
# ai_pr_open's left. lpos anchors the label on that clear vertical run.
LEFT_CH = 300                                    # clear column, left of fix_ci (x≈314..514)
_adr_lefty = GEO["address"][1] + 0.5 * GEO["address"][3]
edge("address", "ai_pr_open", "push ⟳ re-trigger CI + review", C_BLUE, DASH, 0, 0.5, 0, 0.70,
     [(LEFT_CH, _adr_lefty), (LEFT_CH, _aip_y70)], ldx=2, ldy=0, lpos=-0.2)

# escalate (green) + human manages (amber)
# vertical segment is short; offset the label to the RIGHT (ldx) so it doesn't
# blanket the line.
edge("verdict", "needs_human", "ESCALATE (round 5)", C_GREEN, DASH, 0.5, 1, 0.5, 0, ldx=66, ldy=0)
# human manages: needs_human & human_takeover are both on R7, but `approved` sits
# on R7 on the spine between them — a straight line would cross it. Dip the edge
# DOWN to a clear y (between approved's bottom and merge's top) to route around it.
_appr = GEO["approved"]; _merge = GEO["merge"]
_dip = (_appr[1] + _appr[3] + _merge[1]) / 2          # midway between approved bottom & merge top
edge("needs_human", "human_takeover", "human manages", C_AMBER, DASH, 0, 0.5, 1, 0.5,
     [(cxR - 200, _dip), (cxH + 200, _dip)])

# ai-done feedback / prod (amber + purple)
edge("ai_done", "prod", "", C_AMBER, DASH, 0, 0.5, 1, 0.5)                                  # horizontal R9
# steward feedback loop: out ai_done RIGHT, up the far-right channel, into steward
# RIGHT. The corner waypoints must share Y with the exit (bottom corner) and with
# the entry (top corner) so both corners are clean right angles — NOT a y just
# below the exit, which makes the path dip-then-climb (a "soft" double-back hook).
_done_cy = GEO["ai_done"][1] + 0.5 * GEO["ai_done"][3]
_stew_cy = GEO["steward"][1] + 0.5 * GEO["steward"][3]
edge("ai_done", "steward", "mines merged PRs → invariants / ADRs", C_PURPLE, DASH, 1, 0.5, 1, 0.5,
     [(1250, _done_cy), (1250, _stew_cy)])

def shift_nodes(node_ids, dx=0, dy=0):
    """Move a set of nodes by (dx,dy) AND shift every waypoint that belongs to an
    edge touching only moved nodes, plus the moved-endpoint side of edges that
    cross the boundary. Lets you open a gap (e.g. everything below the gate) by
    one call instead of re-hand-editing coordinates.

    Usage: shift_nodes(below_gate, dy=40)  # push the lower half down 40px
    """
    ids = set(node_ids)
    # 1) move the nodes themselves
    for i, n in enumerate(nodes):
        if n[0] in ids:
            nid, x, y, w, h, base, rs, label = n
            nodes[i] = (nid, x + dx, y + dy, w, h, base, rs, label)
            GEO[nid] = (x + dx, y + dy, w, h)
    # 2) move waypoints. A waypoint moves if BOTH endpoints moved (internal edge);
    #    for boundary edges (one endpoint moved) we shift only the waypoints on the
    #    moved node's side — detected by proximity to that node's centre axis.
    for e in E:
        s_in, t_in = e["s"] in ids, e["t"] in ids
        if not e["pts"]:
            continue
        if s_in and t_in:
            e["pts"] = [(px + dx, py + dy) for px, py in e["pts"]]
        elif s_in ^ t_in:
            # shift waypoints closer to the moved endpoint than to the fixed one
            moved = e["s"] if s_in else e["t"]
            mx, my, mw, mh = GEO[moved]
            mcx, mcy = mx + mw / 2, my + mh / 2
            fixed = e["t"] if s_in else e["s"]
            fx, fy, fw, fh = GEO[fixed]
            fcx, fcy = fx + fw / 2, fy + fh / 2
            newpts = []
            for px, py in e["pts"]:
                d_moved = abs(px - mcx) + abs(py - mcy)
                d_fixed = abs(px - fcx) + abs(py - fcy)
                newpts.append((px + dx, py + dy) if d_moved <= d_fixed else (px, py))
            e["pts"] = newpts

# Example: to add MORE space below the gate, uncomment and tune:
#   below_gate = ["ai_pr_open","ci_green","approved","merge","ai_done",
#                 "fix_ci","address","review_pr","verdict","needs_human",
#                 "human_takeover","prod"]
#   shift_nodes(below_gate, dy=30)

# ---- legend (strip below the lanes) ----
LEG_X, LEG_Y, LEG_W, LEG_H = 40, 1135, 1170, 50
legend_items = [
    (C_HAPPY, SOLID, "happy path"),
    (C_AMBER, DASH,  "human loop"),
    (C_BLUE,  DASH,  "CI retry"),
    (C_GREEN, DASH,  "review verdict"),
    (C_PURPLE,DASH,  "steward (meta)"),
]
legend_cells = [
    f'<mxCell id="legend" value="" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#cbd5e1;" '
    f'vertex="1" parent="1"><mxGeometry x="{LEG_X}" y="{LEG_Y}" width="{LEG_W}" height="{LEG_H}" as="geometry"/></mxCell>',
    f'<mxCell id="legend_t" value="&lt;b&gt;Legend&lt;/b&gt;" style="text;html=1;align=left;verticalAlign=middle;fontSize=11;fontColor=#334155;" '
    f'vertex="1" parent="1"><mxGeometry x="{LEG_X+14}" y="{LEG_Y+15}" width="60" height="20" as="geometry"/></mxCell>',
]
lx = LEG_X + 90
for k, (col, dash, text) in enumerate(legend_items):
    d = "dashed=1;dashPattern=6 4;" if dash == DASH else ""
    sw = "2.5" if dash == SOLID else "1.7"
    legend_cells.append(
        f'<mxCell id="legL{k}" style="endArrow=classic;html=1;strokeColor={col};strokeWidth={sw};{d}" edge="1" parent="1">'
        f'<mxGeometry relative="1" as="geometry"><mxPoint x="{lx}" y="{LEG_Y+25}" as="sourcePoint"/>'
        f'<mxPoint x="{lx+42}" y="{LEG_Y+25}" as="targetPoint"/></mxGeometry></mxCell>')
    legend_cells.append(
        f'<mxCell id="legT{k}" value="{escape(text)}" style="text;html=1;align=left;verticalAlign=middle;fontSize=10.5;fontColor=#334155;" '
        f'vertex="1" parent="1"><mxGeometry x="{lx+48}" y="{LEG_Y+15}" width="120" height="20" as="geometry"/></mxCell>')
    lx += 212

# ---- enforce perpendicular entry/exit on every edge (issues 1 & 2) ----
# (run AFTER any shift_nodes so stubs are recomputed against final geometry)
for e in E:
    enforce_perpendicular(e)

# ---- emit XML ----
out = ['<mxfile host="drawio">', '<diagram name="Lifecycle" id="lifecycle">',
       '<mxGraphModel dx="1280" dy="1180" grid="0" page="0" math="0" shadow="0">',
       '<root>', '<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']

for lid, x, y, w, h, fill, stroke, title, tcol in lanes:
    st = f"{RECT}fillColor={fill};strokeColor={stroke};verticalAlign=top;fontStyle=1;fontColor={tcol};fontSize=13;spacingTop=4;"
    out.append(f'<mxCell id="{lid}" value="{escape(title)}" style="{st}" vertex="1" parent="1">'
               f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

# Map each node to the lane that contains it (by horizontal containment), and emit
# it as a CHILD of that lane (parent=laneId) with parent-RELATIVE coords. This
# declares group membership as ground truth: the checker's ESCAPE detector can then
# catch a node that drifts out of its lane, and draw.io keeps children with the lane.
# Nodes spanning all lanes (banner) or outside any lane stay on the default layer.
_lane_box = {lid: (lx, ly, lw, lh) for lid, lx, ly, lw, lh, *_ in lanes}
def _parent_lane(x, w):
    cx = x + w / 2
    for lid, (lx, ly, lw, lh) in _lane_box.items():
        if lx <= cx <= lx + lw and w < lw:        # centre inside lane and node narrower than lane
            return lid
    return None

for nid, x, y, w, h, base, rs, label in nodes:
    fs = "13" if nid == "steward" else "12"
    st = base + rs + f"fontSize={fs};"
    par = _parent_lane(x, w)
    if par:
        plx, ply, _, _ = _lane_box[par]
        rx, ry = x - plx, y - ply            # coords relative to the lane
        out.append(f'<mxCell id="{nid}" value="{label}" style="{st}" vertex="1" parent="{par}">'
                   f'<mxGeometry x="{rx}" y="{ry}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    else:
        out.append(f'<mxCell id="{nid}" value="{label}" style="{st}" vertex="1" parent="1">'
                   f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

for i, e in enumerate(E):
    sw = "2.5" if e["dash"] == SOLID else "1.7"
    dash = "dashed=1;dashPattern=6 4;" if e["dash"] == DASH else ""
    st = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jettySize=auto;"
          f"strokeColor={e['col']};strokeWidth={sw};{dash}"
          f"exitX={e['ex']};exitY={e['ey']};exitDx=0;exitDy=0;"
          f"entryX={e['nx']};entryY={e['ny']};entryDx=0;entryDy=0;"
          f"fontColor={e['col']};fontSize=10;labelBackgroundColor=#ffffff;")
    # x on a relative edge geometry = label position along the path (-1..1)
    xattr = f' x="{e.get("lpos", 0)}"' if e.get("lpos") else ""
    geo = [f'<mxGeometry{xattr} relative="1" as="geometry">']
    if e["pts"]:
        geo.append('<Array as="points">')
        for px, py in e["pts"]:
            geo.append(f'<mxPoint x="{px}" y="{py}"/>')
        geo.append('</Array>')
    # label offset: lift the label OFF the edge so a long label doesn't blanket a
    # whole segment and hide the path. <mxPoint as="offset"> shifts the label.
    if e.get("ldx") or e.get("ldy"):
        geo.append(f'<mxPoint x="{e["ldx"]}" y="{e["ldy"]}" as="offset"/>')
    geo.append('</mxGeometry>')
    out.append(f'<mxCell id="e{i}" value="{escape(e["lbl"])}" style="{st}" edge="1" parent="1" '
               f'source="{e["s"]}" target="{e["t"]}">' + "".join(geo) + '</mxCell>')

out += legend_cells
out += ['</root>', '</mxGraphModel>', '</diagram>', '</mxfile>']
open("process-swimlane.drawio", "w").write("\n".join(out))
print(f"wrote process-swimlane.drawio ({len(nodes)} nodes, {len(E)} edges, {len(lanes)} lanes, legend)")
