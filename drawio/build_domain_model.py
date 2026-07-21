#!/usr/bin/env python3
"""Hand-laid-out domain-model diagram for the issue-agent Python port.

PRECISE ports-and-adapters view (taken from the code). Four lanes, left→right:

  INPUT PORT       WorldState + value objects + Label        world.py, labels.py
  PURE DECISIONS   decide_fix / decide_review / triage /      {fix,review,triage,outcome}.py
                   resolve_followup — each points ONLY to the actions it emits
  ACTIONS (ADT)    one node per Action variant                actions.py
  MUTATION TARGET  the exact domain object each Action        IssueView · PRView · Label
                   mutates (+ git worktree, + the agent port)

Exact mapping (read off the source):
  decide_fix      → Relabel · RunAgent · EnableAutoMerge · CloseIssue · RemoveWorktree
  decide_review   → RunAgent                       (only)
  triage          → PostComment · Relabel          (NOT RunAgent)
  resolve_followup→ PostComment · Relabel
  Relabel ⇒ Label.labels   CloseIssue ⇒ IssueView.state   EnableAutoMerge ⇒ PRView.state
  PostComment ⇒ IssueView|PRView .comments   RunAgent ⇒ AgentTask (agent port)
  RemoveWorktree ⇒ git worktree   NoOp ⇒ ∅
Decision→action edges are coloured by decision; action→target edges are red.
The in-model targets are exactly what WorldState re-reads next poll (top arc).
"""
from html import escape

ROUND = "rounded=1;whiteSpace=wrap;html=1;"
RECT  = "rounded=0;whiteSpace=wrap;html=1;"
def role(fill, stroke, font): return f"fillColor={fill};strokeColor={stroke};fontColor={font};"

VO   = role("#dbeafe", "#2563eb", "#1e3a8a")
ENUM = role("#ede9fe", "#7c3aed", "#5b21b6")
FN   = role("#d1fae5", "#059669", "#065f46")
CORE = role("#ccfbf1", "#0d9488", "#115e59")
ACT  = role("#fee2e2", "#dc2626", "#991b1b")
PORT = role("#e0e7ff", "#4f46e5", "#3730a3")
EXT  = role("#f1f5f9", "#64748b", "#334155")
GATE = role("#fef3c7", "#d97706", "#92400e")
VERD = role("#f1f5f9", "#475569", "#1e293b")

# ---------- lanes (real gutters between each) ----------
LANE_Y, LANE_H = 175, 900
LANES = [
    ("laneIn",  55,  LANE_Y, 300, LANE_H, "#eff6ff", "#bfdbfe",
     "INPUT PORT  ·  WorldState   (world.py · labels.py)", "#1d4ed8"),
    ("laneLog", 395, LANE_Y, 300, LANE_H, "#ecfdf5", "#a7f3d0",
     "PURE DECISIONS  ·  {fix,review,triage,outcome}.py", "#047857"),
    ("laneAct", 800, LANE_Y, 210, LANE_H, "#fef2f2", "#fecaca",
     "ACTIONS  ·  Action ADT", "#b91c1c"),
    ("laneMut", 1115, LANE_Y, 410, LANE_H, "#f8fafc", "#cbd5e1",
     "MUTATION TARGET  ·  the object each Action changes", "#334155"),
]
cxIn, cxLog, cxAct, cxMut = 205, 545, 905, 1310
# gutter A (decisions→actions): logic right edge 695 → action left edge 800.
GA = list(range(705, 796, 9))   # 11 channels, 9px apart
# gutter B (actions→targets): action right edge 1010 → target left edge 1130.
GB = list(range(1018, 1101, 10))
RMARGIN = 1540   # right margin channel (bypass the target stack)
TOP, BOT = 156, 866

def node(nid, cx, w, cy, h, base, rs, *lines):
    label = "&lt;br&gt;".join(lines)
    return (nid, cx - w // 2, cy - h // 2, w, h, base + rs, label)

B="&lt;b&gt;"; b="&lt;/b&gt;"; SM="&lt;font color=#475569&gt;"; sm="&lt;/font&gt;"

# decision rows
DF, DR, DT, DO = 300, 470, 600, 730
# action rows
A_RUN, A_AM, A_CLS, A_PC, A_REL, A_WT, A_NOP = 250, 330, 410, 512, 600, 690, 768
# target rows
T_AT, T_AR, T_PR, T_ISS, T_LBL, T_WT = 250, 330, 452, 560, 666, 760
RG = 952

NODES = [
    ("title", 55, 92, 1470, 56, ROUND + role("#f5f3ff", "#7c3aed", "#5b21b6"),
     f"{B}Autonomous Issue Agent — Domain Model{b}&lt;br&gt;"
     "each decision emits only the Actions it begets; each Action mutates one domain object "
     "(IssueView · PRView · Label) — what WorldState re-reads next poll"),

    # ---- INPUT PORT (boxes 236 wide, centred cxIn+12 → left edge 95, leaving a
    # clear 55..95 gutter for the world→prv composition edge at x=74) ----
    node("world", cxIn + 12, 236, DF - 36, 54, ROUND, VO,
         f"{B}WorldState{b}", f"{SM}issues[] · prs[]{sm}"),
    node("issuev", cxIn + 12, 236, 430, 54, ROUND, VO,
         f"{B}IssueView{b}", f"{SM}labels·comments·state{sm}"),
    node("prv",   cxIn + 12, 236, 545, 54, ROUND, VO,
         f"{B}PRView{b}", f"{SM}reviews·checks·labels·state{sm}"),
    node("parts", cxIn + 12, 226, 648, 46, ROUND, VO,
         f"{B}Review·Check·Comment{b}", f"{SM}leaf VOs{sm}"),
    node("label", cxIn + 12, 236, 748, 54, ROUND, ENUM,
         f"{B}Label{b}  {SM}enum{sm}", f"{SM}ai-ready→working→…{sm}"),

    # ---- PURE DECISIONS ----
    node("dfix",  cxLog, 290, DF, 66, ROUND, FN,
         f"{B}decide_fix(){b}  {SM}fix{sm}",
         f"{SM}reap·dispatch·automerge·cleanup{sm}",
         f"{SM}uses ci_state · latest_other_review{sm}"),
    node("drev",  cxLog, 290, DR, 56, ROUND, FN,
         f"{B}decide_review(){b}  {SM}review{sm}",
         f"{SM}initial / re-review on SHA{sm}"),
    node("triag", cxLog, 290, DT, 56, ROUND, FN,
         f"{B}triage · actions_for_verdict{b}",
         f"{SM}untriaged + updatedAt dedupe{sm}"),
    node("outc",  cxLog, 290, DO, 58, ROUND, CORE,
         f"{B}resolve_followup{b}  {SM}outcome{sm}",
         f"{SM}AgentResult → follow-up Actions{sm}"),

    # ---- ACTIONS ----
    node("a_run", cxAct, 190, A_RUN, 44, ROUND, ACT, f"{B}RunAgent{b}"),
    node("a_am",  cxAct, 190, A_AM,  44, ROUND, ACT, f"{B}EnableAutoMerge{b}"),
    node("a_cls", cxAct, 190, A_CLS, 44, ROUND, ACT, f"{B}CloseIssue{b}"),
    node("a_pc",  cxAct, 190, A_PC,  44, ROUND, ACT, f"{B}PostComment{b}"),
    node("a_rel", cxAct, 190, A_REL, 44, ROUND, ACT, f"{B}Relabel{b}"),
    node("a_wt",  cxAct, 190, A_WT,  44, ROUND, ACT, f"{B}RemoveWorktree{b}"),
    node("a_nop", cxAct, 190, A_NOP, 42, ROUND, ACT, f"{B}NoOp{b}"),

    # ---- MUTATION TARGET ----
    node("t_at",  cxMut, 350, T_AT, 50, ROUND, PORT,
         f"{B}AgentTask{b}  {SM}agent port — out{sm}", f"{SM}prompt·schema·tools·model·cwd{sm}"),
    node("t_ar",  cxMut, 350, T_AR, 50, ROUND, PORT,
         f"{B}AgentResult{b}  {SM}agent port — in{sm}", f"{SM}is_error·status·needs{sm}"),
    node("t_pr",  cxMut, 350, T_PR, 54, ROUND, VO,
         f"{B}PRView{b}", f"{SM}.state→MERGED · .comments{sm}"),
    node("t_iss", cxMut, 350, T_ISS, 54, ROUND, VO,
         f"{B}IssueView{b}", f"{SM}.state open→closed · .comments{sm}"),
    node("t_lbl", cxMut, 350, T_LBL, 50, ROUND, ENUM,
         f"{B}Label{b}  {SM}on Issue/PR .labels{sm}", f"{SM}remove / add{sm}"),
    node("t_wt",  cxMut, 350, T_WT, 50, ROUND, EXT,
         f"{B}git worktree{b}  {SM}external{sm}", f"{SM}cwd removed{sm}"),

    # ---- gate rail ----
    node("gatein", cxIn, 290, RG, 56, ROUND, VERD,
         f"{B}command + GateContext{b}", f"{SM}branch·log·diff·markers·test cmd{sm}"),
    node("gate",  cxLog, 290, RG, 60, ROUND, GATE,
         f"{B}gate{b}  {SM}check-*.sh, pure{sm}",
         f"{SM}branch·TDD·PR body·red-fail·suppression{sm}"),
    node("verdict", cxMut, 350, RG, 56, ROUND, VERD,
         f"{B}Allow | Deny(reason){b}", f"{SM}shown to the agent{sm}"),
]

GEO = {n[0]: (n[1], n[2], n[3], n[4]) for n in NODES}
def _fp(nid, fx, fy):
    x, y, w, h = GEO[nid]; return (x + fx * w, y + fy * h)
def _normal(fx, fy): return "h" if fx in (0, 1) else "v"

# ---------- edges ----------
SOLID, DASH = "0", "1"
AGG  = "#64748b"; MUT = "#dc2626"; SDKB = "#9333ea"; GATEC = "#d97706"
C_FIX, C_REV, C_TRI, C_OUT = "#1e293b", "#7c3aed", "#0d9488", "#ca8a04"
E = []
def edge(s, t, lbl, col, dash, ex, ey, nx, ny, pts=None, ldx=0, ldy=0, lpos=None):
    E.append(dict(s=s, t=t, lbl=lbl, col=col, dash=dash, ex=ex, ey=ey, nx=nx, ny=ny,
                  pts=list(pts or []), ldx=ldx, ldy=ldy, lpos=lpos))

def da(src, sy, tgt, ty, ch, col, lbl):
    """decision→action via gutter-A channel GA[ch]: exit src right at frac sy, up/down
    the channel, into tgt left at frac ty."""
    x = GA[ch]
    edge(src, tgt, lbl, col, SOLID, 1, sy, 0, ty,
         [(x, _fp(src, 1, sy)[1]), (x, _fp(tgt, 0, ty)[1])], lpos=-0.62, ldy=-8)

def at(src, sy, tgt, ty, ch, dash=SOLID, lbl=""):
    """action→target via gutter-B channel GB[ch]."""
    x = GB[ch]
    edge(src, tgt, lbl, MUT, dash, 1, sy, 0, ty,
         [(x, _fp(src, 1, sy)[1]), (x, _fp(tgt, 0, ty)[1])], lpos=-0.55, ldy=-8)

# ----- INPUT PORT aggregation -----
edge("world", "issuev", "issues 1..*", AGG, SOLID, 0.5, 1, 0.5, 0, ldx=52, ldy=0)
edge("world", "prv", "prs 1..*", AGG, SOLID, 0, 0.5, 0, 0.5, [(74, DF - 36), (74, 545)], ldx=2, ldy=-10)
edge("prv", "parts", "1..*", AGG, SOLID, 0.5, 1, 0.5, 0, ldx=26, ldy=0)

# ----- World → deciders (read). Dogleg through the input→logic gutter (x≈368/378/
# 388) so each enters its decider's left face level. -----
edge("world", "dfix", "read", AGG, SOLID, 1, 0.5, 0, 0.5, [(368, _fp("world", 1, 0.5)[1]), (368, DF)], ldy=-10)
edge("world", "drev", "read", AGG, SOLID, 1, 0.7, 0, 0.5, [(378, DR)], ldy=-10)
edge("world", "triag", "read", AGG, SOLID, 1, 0.85, 0, 0.5, [(388, DT)], ldy=-10)

# ----- DECISION → ACTION (exact emit sets) -----
# decide_fix → 5 actions. channels 0..4, ordered by target row.
da("dfix", 0.30, "a_run", 0.5, 0, C_FIX, "RunAgent")
da("dfix", 0.42, "a_am",  0.5, 1, C_FIX, "EnableAutoMerge")
da("dfix", 0.55, "a_cls", 0.5, 2, C_FIX, "CloseIssue")
da("dfix", 0.70, "a_rel", 0.28, 3, C_FIX, "Relabel")
da("dfix", 0.82, "a_wt",  0.5, 4, C_FIX, "RemoveWorktree")
# decide_review → RunAgent only.
da("drev", 0.5, "a_run", 0.72, 5, C_REV, "RunAgent")
# triage → PostComment, Relabel.
da("triag", 0.40, "a_pc", 0.30, 6, C_TRI, "PostComment")
da("triag", 0.60, "a_rel", 0.55, 7, C_TRI, "Relabel")
# resolve_followup → PostComment, Relabel.
da("outc", 0.40, "a_pc", 0.72, 8, C_OUT, "PostComment")
da("outc", 0.60, "a_rel", 0.80, 9, C_OUT, "Relabel")

# ----- ACTION → MUTATION TARGET (exact field) -----
edge("a_run", "t_at", "→ AgentTask", MUT, SOLID, 1, 0.5, 0, 0.5, ldy=-8)
at("a_am",  0.5, "t_pr", 0.30, 0, SOLID, "PRView.state→MERGED")
at("a_cls", 0.5, "t_iss", 0.30, 1, SOLID, "IssueView.state")
at("a_pc",  0.40, "t_iss", 0.70, 2, SOLID, "Issue .comments")
at("a_pc",  0.60, "t_pr", 0.70, 3, DASH,  "or PR .comments")
at("a_rel", 0.5, "t_lbl", 0.5, 4, SOLID, "Label .labels")
at("a_wt",  0.5, "t_wt", 0.5, 5, SOLID, "remove")

# ----- agent port round-trip + feedback -----
edge("t_at", "t_ar", "AgentRunner · Claude SDK", SDKB, DASH, 0.5, 1, 0.5, 0, ldx=14, ldy=0)
# AgentResult → resolve_followup: exit t_ar RIGHT to the margin channel, down the
# bottom corridor, back into outc's bottom (bypasses the whole target stack).
edge("t_ar", "outc", "AgentResult", C_OUT, SOLID, 1, 0.5, 0.5, 1,
     [(RMARGIN, T_AR), (RMARGIN, BOT), (cxLog, BOT)], lpos=-0.86, ldy=-9)

# ----- THE LOOP: mutated objects re-read into WorldState next poll -----
# exit t_lbl RIGHT → margin channel → up over the top → left to world's top.
edge("t_lbl", "world", "next poll: the adapter re-reads the mutated GitHub state into WorldState",
     AGG, DASH, 1, 0.5, 0.5, 0,
     [(RMARGIN + 20, T_LBL), (RMARGIN + 20, TOP), (cxIn + 12, TOP)], lpos=0.5, ldy=-9)

# ----- gate rail -----
edge("gatein", "gate", "", GATEC, SOLID, 1, 0.5, 0, 0.5)
edge("gate", "verdict", "", GATEC, SOLID, 1, 0.5, 0, 0.5)

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
       '<mxGraphModel dx="1500" dy="1000" grid="0" page="0" math="0" shadow="0">',
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
    sw = "1.6" if e["col"] == MUT else ("1.7" if e["dash"] == DASH else "1.8")
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
LEG_Y = LANE_Y + LANE_H + 22
leg = [
    ("decide_fix emits", C_FIX, SOLID, 0, 0),
    ("decide_review emits", C_REV, SOLID, 0, 1),
    ("triage emits", C_TRI, SOLID, 0, 2),
    ("resolve_followup emits", C_OUT, SOLID, 0, 3),
    ("Action ⇒ mutates object", MUT, SOLID, 1, 0),
    ("aggregation · next-poll re-read", AGG, DASH, 1, 1),
    ("agent round-trip (SDK)", SDKB, DASH, 1, 2),
    ("gate guard", GATEC, SOLID, 1, 3),
]
PITCH, SAMPLE, TXT_W, ROWH = 350, 32, 250, 26
out.append(f'<mxCell id="legbox" value="Legend" style="{RECT}fillColor=#ffffff;strokeColor=#cbd5e1;'
           f'verticalAlign=top;fontStyle=1;fontColor=#475569;fontSize=11;spacingTop=4;align=left;spacingLeft=8;" '
           f'vertex="1" parent="1"><mxGeometry x="55" y="{LEG_Y}" width="{PITCH*4+30}" height="{ROWH*2+30}" as="geometry"/></mxCell>')
for j, (txt, col, dash, r, c) in enumerate(leg):
    lx = 72 + c * PITCH
    sy = LEG_Y + 34 + r * ROWH
    dd = "dashed=1;dashPattern=6 4;" if dash == DASH else ""
    out.append(f'<mxCell id="legs{j}" style="endArrow=classic;html=1;strokeColor={col};strokeWidth=2;{dd}" '
               f'edge="1" parent="1"><mxGeometry relative="1" as="geometry">'
               f'<mxPoint x="{lx}" y="{sy}" as="sourcePoint"/>'
               f'<mxPoint x="{lx+SAMPLE}" y="{sy}" as="targetPoint"/></mxGeometry></mxCell>')
    out.append(f'<mxCell id="legt{j}" value="{escape(txt)}" '
               f'style="text;html=1;align=left;verticalAlign=middle;fontSize=10;fontColor=#334155;" '
               f'vertex="1" parent="1"><mxGeometry x="{lx+SAMPLE+6}" y="{sy-10}" width="{TXT_W}" height="20" as="geometry"/></mxCell>')

out += ['</root>', '</mxGraphModel>', '</diagram>', '</mxfile>']
open("drawio/domain-model.drawio", "w").write("\n".join(out))
print(f"wrote drawio/domain-model.drawio ({len(NODES)} nodes, {len(E)} edges, {len(LANES)} lanes)")
