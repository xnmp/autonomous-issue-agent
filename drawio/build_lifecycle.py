#!/usr/bin/env python3
"""Hand-laid-out LIFECYCLE diagram: the domain as a state machine.

Complements domain-model.svg (the static type/port view). Behavioural view, like
the reference process diagram, but PRECISE: every edge is labelled with the actual
domain Action that performs the mutation, and every diamond is the pure decision
function that fires on a state.

Layout = a central SPINE alternating state-pill / decision-diamond (the happy
path runs straight down it); off-path states (ai-triage, ai-blocked) + human
touch-points peel LEFT; the PR review/CI sub-cycle sits in the RIGHT lane.

  STATE pills    = Label lifecycle value on IssueView / PRView
  DECISION       = decide_fix / triage / resolve_followup / decide_review / cleanup
  EDGE labels    = the Action that mutates state:
                     Relabel(from→to) · RunAgent(kind) · PostComment ·
                     CloseIssue · EnableAutoMerge   (⊕ = emitted together)
"""
from html import escape

ROUND = "rounded=1;whiteSpace=wrap;html=1;"
PILL  = "rounded=1;arcSize=45;whiteSpace=wrap;html=1;"
RECT  = "rounded=0;whiteSpace=wrap;html=1;"
DEC   = "rhombus;whiteSpace=wrap;html=1;"
def role(fill, stroke, font): return f"fillColor={fill};strokeColor={stroke};fontColor={font};"

ST   = role("#e2e8f0", "#475569", "#0f172a")   # lifecycle state pill (on spine)
STd  = role("#dcfce7", "#16a34a", "#14532d")   # terminal (done)
STw  = role("#fef9c3", "#ca8a04", "#713f12")   # waiting-on-human (triage/blocked)
DI   = DEC + role("#d1fae5", "#059669", "#065f46")   # issue-side decision fn
DR   = DEC + role("#ede9fe", "#7c3aed", "#5b21b6")   # PR-side decision fn
HUM  = role("#ffedd5", "#ea580c", "#9a3412")   # human action
MRG  = role("#dbeafe", "#2563eb", "#1e3a8a")   # GitHub-side (merge)

# ---------- lanes ----------
LANE_Y, LANE_H = 178, 880
LANES = [
    ("laneL", 60,  LANE_Y, 300, LANE_H, "#fffbeb", "#fde68a",
     "OFF-PATH  ·  waiting on a human", "#b45309"),
    ("laneC", 380, LANE_Y, 360, LANE_H, "#f8fafc", "#cbd5e1",
     "LIFECYCLE SPINE  ·  Label state ▸ pure decision ▸ next state", "#334155"),
    ("laneR", 760, LANE_Y, 540, LANE_H, "#faf5ff", "#e9d5ff",
     "PR REVIEW / CI SUB-CYCLE  ·  decide_review · decide_fix(PR) · cleanup", "#7c3aed"),
]
cxL, cxC, cxR = 210, 560, 980
cxR2 = 1190   # second right column

# ---------- spine rows (alternating state / decision) ----------
R1, R2, R3, R4, R5, R6, R7, R8, R9 = 248, 338, 430, 520, 612, 704, 798, 890, 958

def node(nid, cx, w, cy, h, base, rs, *lines):
    label = "&lt;br&gt;".join(lines)
    return (nid, cx - w // 2, cy - h // 2, w, h, base + rs, label)

B="&lt;b&gt;"; b="&lt;/b&gt;"; SM="&lt;font color=#475569&gt;"; sm="&lt;/font&gt;"

NODES = [
    ("title", 60, 92, 1240, 56, ROUND + role("#f5f3ff", "#7c3aed", "#5b21b6"),
     f"{B}Autonomous Issue Agent — Lifecycle (domain state machine){b}&lt;br&gt;"
     "pills = Label states · diamonds = pure decision functions · edge labels = the Action that mutates state"),

    # ---- central spine (state, decision, state, …) ----
    node("s_open", cxC, 230, R1, 50, PILL, ST, f"{B}open issue{b}", f"{SM}no lifecycle label{sm}"),
    node("d_tri",  cxC, 150, R2, 74, "", DI, f"{B}triage{b}", f"{SM}steward{sm}"),
    node("s_rdy",  cxC, 200, R3, 46, PILL, ST, f"{B}ai-ready{b}"),
    node("d_fix",  cxC, 160, R4, 78, "", DI, f"{B}decide_fix{b}", f"{SM}pick_ready{sm}"),
    node("s_wrk",  cxC, 200, R5, 46, PILL, ST, f"{B}ai-working{b}"),
    node("d_res",  cxC, 170, R6, 86, "", DI, f"{B}resolve_{b}", f"{B}followup{b}", f"{SM}envelope{sm}"),
    node("s_pro",  cxC, 220, R7, 50, PILL, ST, f"{B}ai-pr-open{b}", f"{SM}issue + PR{sm}"),
    node("s_done", cxC, 220, R9, 50, PILL, STd, f"{B}ai-done{b}", f"{SM}issue closed{sm}"),

    # ---- off-path (left): waiting on a human ----
    node("s_tri",  cxL, 190, R2, 46, PILL, STw, f"{B}ai-triage{b}"),
    node("s_blk",  cxL, 190, R6, 46, PILL, STw, f"{B}ai-blocked{b}"),
    node("h_rep",  cxL, 190, R7, 50, ROUND, HUM, f"{B}human reply{b}", f"{SM}unblocks{sm}"),

    # ---- PR review / CI sub-cycle (right) ----
    node("d_rev",  cxR, 180, R7, 78, "", DR, f"{B}decide_review{b}"),
    node("d_prf",  cxR, 190, R5, 86, "", DR, f"{B}decide_fix{b}", f"{SM}PR branch{sm}",
         f"{SM}ci_state · review{sm}"),
    node("d_am",   cxR2, 150, R8 - 40, 60, ROUND, MRG, f"{B}EnableAutoMerge{b}",
         f"{SM}APPROVE + ci ok{sm}"),
    node("d_cln",  cxR, 165, R9, 72, "", DR, f"{B}cleanup{b}"),
]

GEO = {n[0]: (n[1], n[2], n[3], n[4]) for n in NODES}
def _fp(nid, fx, fy):
    x, y, w, h = GEO[nid]; return (x + fx * w, y + fy * h)
def _normal(fx, fy): return "h" if fx in (0, 1) else "v"

# ---------- edges ----------
SOLID, DASH = "0", "1"
HAPPY = "#1e293b"; RELAB = "#0d9488"; REVC = "#7c3aed"; LOOPB = "#2563eb"; HUMC = "#ea580c"; MRGC = "#2563eb"
E = []
def edge(s, t, lbl, col, dash, ex, ey, nx, ny, pts=None, ldx=0, ldy=0, lpos=None):
    E.append(dict(s=s, t=t, lbl=lbl, col=col, dash=dash, ex=ex, ey=ey, nx=nx, ny=ny,
                  pts=list(pts or []), ldx=ldx, ldy=ldy, lpos=lpos))

# ===== happy-path spine (straight down cxC) =====
edge("s_open", "d_tri", "", HAPPY, SOLID, 0.5, 1, 0.5, 0)
edge("d_tri",  "s_rdy", "ready+promote → Relabel(→ai-ready)", HAPPY, SOLID, 0.5, 1, 0.5, 0, lpos=0.1, ldx=92, ldy=0)
edge("s_rdy",  "d_fix", "", HAPPY, SOLID, 0.5, 1, 0.5, 0)
edge("d_fix",  "s_wrk", "Relabel(ai-ready→ai-working) ⊕ RunAgent(RESOLVE)", HAPPY, SOLID, 0.5, 1, 0.5, 0, lpos=0.1, ldx=128, ldy=0)
edge("s_wrk",  "d_res", "AgentResult", HAPPY, SOLID, 0.5, 1, 0.5, 0, lpos=0.1, ldx=40, ldy=0)
edge("d_res",  "s_pro", "PR_OPENED → Relabel(ai-working→ai-pr-open)", HAPPY, SOLID, 0.5, 1, 0.5, 0, lpos=0.1, ldx=118, ldy=0)

# ===== triage: blocked branch → ai-triage (left); reply re-triages/promotes =====
edge("d_tri",  "s_tri", "blocked → PostComment(@author) ⊕ Relabel(+ai-triage)", RELAB, SOLID,
     0, 0.5, 1, 0.5, lpos=0, ldy=-9)
edge("s_tri",  "s_rdy", "human promote", HUMC, DASH, 0.5, 1, 0, 0.7,
     [(cxL, R3), (380 + 6, R3)], lpos=0.2, ldy=-9)

# ===== resolve_followup branches: blocked (left) and retry (loop to ai-ready) =====
edge("d_res",  "s_blk", "BLOCKED → PostComment(needs) ⊕ Relabel(→ai-blocked)", RELAB, SOLID,
     0, 0.5, 1, 0.5, lpos=0, ldy=-9)
edge("d_res",  "s_rdy", "RETRY (error) → Relabel(ai-working→ai-ready)", LOOPB, DASH,
     1, 0.5, 1, 0.7, [(720, R6), (720, R3)], lpos=0, ldx=2, ldy=-9)

# ===== blocked → human reply → RESUME back to ai-working =====
edge("s_blk",  "h_rep", "", HUMC, DASH, 0.5, 1, 0.5, 0)
# RESUME loop: exit human-reply RIGHT, route up a gutter at x=345 (between the left
# lane and the spine). Label rides the long vertical run; kept short to fit.
edge("h_rep",  "s_wrk", "RunAgent(RESUME) ⊕ Relabel(→ai-working)", HUMC, DASH,
     1, 0.5, 0, 0.5, [(345, R7), (345, R5)], lpos=0, ldx=2, ldy=0)

# ===== reap orphaned ai-working → ai-ready (recovery) =====
edge("s_wrk",  "s_rdy", "reap orphan → Relabel(ai-working→ai-ready)", LOOPB, DASH,
     1, 0.5, 1, 0.3, [(688, R5), (688, R3)], lpos=-0.5, ldx=2, ldy=-11)

# ===== PR review / CI sub-cycle (right) =====
edge("s_pro",  "d_rev", "RunAgent(REVIEW / RE_REVIEW)", REVC, SOLID, 1, 0.5, 0, 0.5, lpos=0, ldy=-11)
# REQUEST_CHANGES / CI failure → decide_fix(PR) → push, stays ai-pr-open (loop).
edge("d_rev",  "d_prf", "REQUEST_CHANGES", REVC, SOLID, 0.5, 0, 0.5, 1, lpos=0, ldx=46, ldy=0)
edge("d_prf",  "s_pro", "RunAgent(ADDRESS_REVIEW)/RunAgent(FIX_CI) → push", REVC, DASH,
     0, 0.5, 1, 0.25, [(cxR - 150, R5), (cxR - 150, _fp("s_pro", 1, 0.25)[1])], lpos=-0.1, ldy=-9)
# APPROVE + green → EnableAutoMerge → GitHub merges → cleanup.
edge("d_rev",  "d_am",  "APPROVE + ci∈{ok,none}", REVC, SOLID, 1, 0.5, 0, 0.5,
     [(1100, R7)], lpos=-0.15, ldy=-9)
edge("d_am",   "d_cln", "GitHub merges PR", MRGC, SOLID, 0.5, 1, 1, 0.5,
     [(cxR2, R9 + 4)], lpos=0.1, ldy=-9)
# merged → ai-done (+CloseIssue); closed-unmerged → ai-ready (re-queue).
edge("d_cln",  "s_done", "merged → Relabel(ai-pr-open→ai-done) ⊕ CloseIssue", HAPPY, SOLID,
     0.35, 1, 1, 0.5, [(cxR - 60, R9 + 64), (cxC + 130, R9 + 64)], lpos=0.25, ldy=-9)
# closed-unmerged re-queue: exit cleanup LEFT, run UP the lane-gutter at x=400
# (clear of every spine node), into ai-ready's LEFT face. One long visible loop.
# closed-unmerged re-queue: exit cleanup's BOTTOM, drop below the whole diagram,
# run left to the far gutter (x=400, clear of every node), up into ai-ready's left.
edge("d_cln",  "s_rdy", "closed unmerged → Relabel(→ai-ready)", LOOPB, DASH,
     0.65, 1, 0, 0.3, [(cxR + 40, R9 + 96), (400, R9 + 96), (400, R3)], lpos=-0.82, ldy=-9)

# ---------- perpendicular pass ----------
def _simplify_ortho(poly, tol=1.0):
    out = [poly[0]]
    for p in poly[1:]:
        if abs(p[0]-out[-1][0]) > tol or abs(p[1]-out[-1][1]) > tol:
            out.append(p)
    poly = out
    changed = True
    while changed and len(poly) >= 3:
        changed = False
        for i in range(1, len(poly)-1):
            a, b2, c = poly[i-1], poly[i], poly[i+1]
            coll = (abs(a[0]-b2[0]) < tol and abs(b2[0]-c[0]) < tol) or \
                   (abs(a[1]-b2[1]) < tol and abs(b2[1]-c[1]) < tol)
            if coll:
                poly.pop(i); changed = True; break
            if abs(a[0]-c[0]) < tol and abs(a[1]-c[1]) < tol:
                poly.pop(i+1); poly.pop(i); changed = True; break
    return poly

def clean_orthogonal(sp, pts, tp, sn, tn):
    poly = [sp] + list(pts) + [tp]
    if len(poly) >= 2:
        nxt = poly[1]
        if sn == "v" and abs(nxt[0]-sp[0]) > .5: poly.insert(1, (sp[0], nxt[1]))
        elif sn == "h" and abs(nxt[1]-sp[1]) > .5: poly.insert(1, (nxt[0], sp[1]))
    if len(poly) >= 2:
        prv = poly[-2]
        if tn == "v" and abs(prv[0]-tp[0]) > .5: poly.insert(len(poly)-1, (tp[0], prv[1]))
        elif tn == "h" and abs(prv[1]-tp[1]) > .5: poly.insert(len(poly)-1, (prv[0], tp[1]))
    return _simplify_ortho(poly)[1:-1]

def enforce_perpendicular(e):
    sp, tp = _fp(e["s"], e["ex"], e["ey"]), _fp(e["t"], e["nx"], e["ny"])
    sn, tn = _normal(e["ex"], e["ey"]), _normal(e["nx"], e["ny"])
    e["pts"] = clean_orthogonal(sp, e["pts"], tp, sn, tn)

for e in E:
    enforce_perpendicular(e)

# ---------- emit ----------
out = ['<mxfile host="drawio">', '<diagram name="Page-1" id="p1">',
       '<mxGraphModel dx="1400" dy="1000" grid="0" page="0" math="0" shadow="0">',
       '<root>', '<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']

for lid, x, y, w, h, fill, stroke, title, tcol in LANES:
    st = (f"{RECT}fillColor={fill};strokeColor={stroke};verticalAlign=top;fontStyle=1;"
          f"fontColor={tcol};fontSize=12;spacingTop=4;")
    out.append(f'<mxCell id="{lid}" value="{escape(title)}" style="{st}" vertex="1" parent="1">'
               f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

for nid, x, y, w, h, st, label in NODES:
    out.append(f'<mxCell id="{nid}" value="{label}" style="{st}fontSize=11;align=center;" '
               f'vertex="1" parent="1">'
               f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

for i, e in enumerate(E):
    sw = "2.4" if e["dash"] == SOLID and e["col"] == HAPPY else ("1.6" if e["dash"] == DASH else "1.8")
    d = "dashed=1;dashPattern=6 4;" if e["dash"] == DASH else ""
    st = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jettySize=auto;"
          f"strokeColor={e['col']};strokeWidth={sw};{d}"
          f"exitX={e['ex']};exitY={e['ey']};exitDx=0;exitDy=0;"
          f"entryX={e['nx']};entryY={e['ny']};entryDx=0;entryDy=0;"
          f"fontColor={e['col']};fontSize=9;labelBackgroundColor=#ffffff;")
    lp = f' x="{e["lpos"]}"' if e["lpos"] is not None else ""
    geo = f'<mxGeometry{lp} relative="1" as="geometry">'
    if e["pts"]:
        geo += '<Array as="points">' + "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in e["pts"]) + '</Array>'
    if e["ldx"] or e["ldy"]:
        geo += f'<mxPoint x="{e["ldx"]}" y="{e["ldy"]}" as="offset"/>'
    geo += '</mxGeometry>'
    out.append(f'<mxCell id="e{i}" value="{escape(e["lbl"])}" style="{st}" edge="1" parent="1" '
               f'source="{e["s"]}" target="{e["t"]}">{geo}</mxCell>')

# ---------- legend ----------
LEG_Y = LANE_Y + LANE_H + 24
leg = [
    ("happy path", HAPPY, SOLID, 0, 0),
    ("issue-side mutation (Relabel/Comment)", RELAB, SOLID, 0, 1),
    ("review / PR-side", REVC, SOLID, 0, 2),
    ("recovery · retry · re-queue loop", LOOPB, DASH, 1, 0),
    ("human-initiated", HUMC, DASH, 1, 1),
    ("⊕ = Actions emitted together", "#475569", None, 1, 2),
]
PITCH, SAMPLE, TXT_W, ROWH = 360, 34, 300, 26
out.append(f'<mxCell id="legbox" value="Legend" style="{RECT}fillColor=#ffffff;strokeColor=#cbd5e1;'
           f'verticalAlign=top;fontStyle=1;fontColor=#475569;fontSize=11;spacingTop=4;align=left;spacingLeft=8;" '
           f'vertex="1" parent="1"><mxGeometry x="60" y="{LEG_Y}" width="{PITCH*3+40}" height="{ROWH*2+30}" as="geometry"/></mxCell>')
for j, (txt, col, dash, r, c) in enumerate(leg):
    lx = 80 + c * PITCH
    sy = LEG_Y + 34 + r * ROWH
    if dash is None:
        out.append(f'<mxCell id="legt{j}" value="{escape(txt)}" '
                   f'style="text;html=1;align=left;verticalAlign=middle;fontSize=10;fontColor=#334155;fontStyle=2;" '
                   f'vertex="1" parent="1"><mxGeometry x="{lx}" y="{sy-10}" width="{TXT_W}" height="20" as="geometry"/></mxCell>')
        continue
    dd = "dashed=1;dashPattern=6 4;" if dash == DASH else ""
    out.append(f'<mxCell id="legs{j}" style="endArrow=classic;html=1;strokeColor={col};strokeWidth=2;{dd}" '
               f'edge="1" parent="1"><mxGeometry relative="1" as="geometry">'
               f'<mxPoint x="{lx}" y="{sy}" as="sourcePoint"/>'
               f'<mxPoint x="{lx+SAMPLE}" y="{sy}" as="targetPoint"/></mxGeometry></mxCell>')
    out.append(f'<mxCell id="legt{j}" value="{escape(txt)}" '
               f'style="text;html=1;align=left;verticalAlign=middle;fontSize=10;fontColor=#334155;" '
               f'vertex="1" parent="1"><mxGeometry x="{lx+SAMPLE+8}" y="{sy-10}" width="{TXT_W}" height="20" as="geometry"/></mxCell>')

out += ['</root>', '</mxGraphModel>', '</diagram>', '</mxfile>']
open("drawio/lifecycle.drawio", "w").write("\n".join(out))
print(f"wrote drawio/lifecycle.drawio ({len(NODES)} nodes, {len(E)} edges, {len(LANES)} lanes)")
