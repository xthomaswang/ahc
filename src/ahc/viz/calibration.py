"""The calibration page (/calibration) and its routes, served only by the dashboard the AHC server process serves
(the standalone ahc-dashboard has no server to calibrate for).

It is the person's page: slot calibration (each camera's boxes over a fresh frame: still right, adjust and save, or skip) and
plate calibration (click four wells of a plate the agent asked about; a perspective grid follows). What it does is in
ahc.calibration. The page carries the emergency stop bar. Every route sits behind the same Host/Origin checks as the stop
and behind the control token for what writes; the view-only dashboard has none of it (its run page shows one line that
calibration is in progress).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ahc.calibration import Calibration
from ahc.core.errors import LabError
from ahc.viz import stopbar

NAME = r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}"
CAMERA = re.compile(rf"/calibration/({NAME})/(frame\.jpg|frame|still-right|skip|boxes)")
PLATE = re.compile(rf"/calibration/plate/({NAME})/save")

CODES = {"unknown_camera": 404, "no_slot_map": 409, "no_frame": 409, "no_plate_request": 404, "no_earlier_corners": 409,
         "bad_slot_map": 400, "bad_corners": 400, "box_outside_frame": 400, "cannot_compare": 409}


def error(exc: LabError) -> tuple[int, dict[str, Any]]:
  return CODES.get(exc.code, 409), {"error": exc.message, "hint": exc.hint, "code": exc.code}


def get(cal: Calibration, route: str) -> tuple[int, bytes, str] | tuple[int, dict[str, Any]] | None:
  """GET /calibration/state and /calibration/<camera>/frame.jpg -> (200, bytes, content type) or (status, json); None: not ours."""
  if route == "/calibration/state":
    return 200, json.dumps(cal.public()).encode(), "application/json"
  m = CAMERA.fullmatch(route)
  if m and m.group(2) == "frame.jpg":
    data = cal.frame_bytes(m.group(1))
    return (200, data, "image/jpeg") if data else (404, {"error": "no picture taken yet"})
  return None


def post(cal: Calibration, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
  """The person's actions; None: not a calibration route. Run on the server's loop (Calibration.call)."""
  try:
    m = CAMERA.fullmatch(path)
    if m and m.group(2) != "frame.jpg":
      name, action = m.groups()
      if action == "frame":
        return 200, cal.call(cal.look(name))
      if action == "still-right":
        return 200, cal.call(cal.still_right(name))
      if action == "skip":
        return 200, cal.call(cal.skip(name))
      return 200, cal.call(cal.save_boxes(name, payload.get("slots")))
    m = PLATE.fullmatch(path)
    if m:
      return 200, cal.call(cal.save_plate(m.group(1), payload.get("corners"), earlier=payload.get("earlier") is True))
  except LabError as exc:
    return error(exc)
  return None


def brief(cal: Calibration | None, file_state: dict[str, Any] | None) -> dict[str, Any] | None:
  """What the run page's /state says about calibration: who waits, and why auto may be off."""
  if cal is not None:
    return {"waiting_for_person": cal.for_person(), "active": cal.active(), "stale": cal.stale,
            "plates": {n: p["state"] for n, p in cal.plates_public().items()}, "serves": True}
  if file_state:
    return {"waiting_for_person": file_state.get("waiting_for_person") or [], "active": bool(file_state.get("active")),
            "stale": file_state.get("stale"), "plates": {n: p.get("state") for n, p in (file_state.get("plates") or {}).items()},
            "serves": False}
  return None


CSS = r"""
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--good:#0ca30c;--warning:#fab219;
--critical:#d03b3b;--chip:#f0efec;--meter:#2a78d6}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--chip:#2c2c2a;--meter:#86b6ef}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--chip:#2c2c2a;--meter:#86b6ef}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;padding:16px 24px 8px}
h1{font-size:18px;margin:0;font-weight:600}h2{font-size:15px;margin:0;font-weight:600}.sub{color:var(--ink2)}
a{color:var(--meter)}code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
main{padding:8px 24px 32px;display:grid;gap:16px;max-width:1100px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;min-width:0}
.banner{padding:10px 14px;border-left:3px solid var(--warning);background:var(--chip);border-radius:0 8px 8px 0}
.banner.ok{border-left-color:var(--good)}.banner .more{color:var(--ink2);font-size:13px;margin-top:2px}
.head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}.dev{color:var(--muted);font-size:12px}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;background:var(--chip);color:var(--ink2);font-size:12px}
.chip.need::before{content:"◷ ";color:var(--warning)}.chip.done::before{content:"✓ ";color:var(--good)}.chip.skip::before{content:"– "}
.say{font-size:13px;margin:6px 0}.say.good::before{content:"✓ ";color:var(--good)}.say.warn::before{content:"⚠ ";color:var(--warning)}
.say.err{color:var(--critical)}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
button.b{font:inherit;font-size:13px;background:var(--chip);color:var(--ink);border:1px solid var(--border);border-radius:8px;padding:6px 12px;cursor:pointer}
button.b.main{background:var(--ink);color:var(--surface);border-color:var(--ink)}button.b:disabled{opacity:.45;cursor:default}
svg.work{display:block;width:100%;max-height:560px;border-radius:6px;background:var(--grid);touch-action:none;user-select:none}
svg.work text{fill:#fff;stroke:#000;stroke-width:3px;paint-order:stroke;font-size:15px;pointer-events:none}
svg .box{fill:rgba(0,200,0,.10);stroke:#0ca30c;stroke-width:2;cursor:move}svg .box.tips{stroke:#fab219;fill:rgba(250,178,25,.12)}
svg .handle{fill:#fff;stroke:#0b0b0b;stroke-width:2;cursor:nwse-resize}
svg .slotbox{fill:none;stroke:#0ca30c;stroke-width:2;stroke-dasharray:6 4;pointer-events:none}
svg .well{fill:rgba(42,120,214,.20);stroke:#fff;stroke-width:1.2;pointer-events:none}
svg .pt{fill:#fab219;stroke:#0b0b0b;stroke-width:2;cursor:grab}svg .pt.first{fill:#0ca30c}
.hint{font-size:14px;font-weight:600;margin:6px 0}.empty{color:var(--muted);padding:12px 0}
@media (max-width:600px){header{padding:12px 16px 4px}main{padding:8px 16px 24px}.card{padding:12px}}
"""

BODY = r"""
<header><h1>Calibration</h1><span class="sub">cameras and plates, for the person at the robot</span>
<a href="/?page=run" id="runlink" style="margin-left:auto">Run page</a></header>
<main><section class="banner" id="banner">Loading…</section><div id="plates"></div><div id="cams"></div></main>
"""

JS = r"""
const TOKEN=document.querySelector('meta[name="ahc-token"]').content,VIEW=document.querySelector('meta[name="ahc-view"]').content==="1";
const el=(t,a={},txt)=>{const e=document.createElement(t);for(const[k,v]of Object.entries(a))e.setAttribute(k,v);if(txt!==undefined)e.textContent=txt;return e};
const sv=(t,a={})=>{const e=document.createElementNS("http://www.w3.org/2000/svg",t);for(const[k,v]of Object.entries(a))e.setAttribute(k,v);return e};
__AHC_STOPBAR_JS__
const STATE_TEXT={pending:["need","needs you"],calibrated:["done","calibrated"],confirmed:["done","still right"],skipped:["skip","skipped"],needs_map:["need","waiting for the agent"]};
let ST=null;const cards={},plateCards={},work={},pwork={},tried={},busy={},errors={};
async function api(path,body){let r;try{r=await fetch(path,{method:"POST",headers:{"X-AHC-Control":"1","X-AHC-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body||{})})}
  catch(e){return{ok:false,status:0,j:{error:"the dashboard did not answer"}}}
  let j={};try{j=await r.json()}catch(e){}return{ok:r.ok,status:r.status,j}}
const hhmm=s=>s?String(s).slice(11,16):"";
function viewbox(w,h,pad){return`${-pad} ${-pad} ${w+2*pad} ${h+2*pad}`}
function pointer(svg){return ev=>{const p=svg.createSVGPoint();p.x=ev.clientX;p.y=ev.clientY;const q=p.matrixTransform(svg.getScreenCTM().inverse());return[q.x,q.y]}}

// -- a fresh picture of a camera ---------------------------------------------------------------
async function takeFrame(name){if(busy[name])return;busy[name]=true;tried[name]=true;errors[name]="";repaint();
  const r=await api(`/calibration/${encodeURIComponent(name)}/frame`);busy[name]=false;
  if(!r.ok)errors[name]=`${r.j.error||"no picture"}${r.j.hint?" "+r.j.hint:""}`;await poll()}

// -- slot calibration ----------------------------------------------------------------------------
function cameraCard(host,c){
  host.replaceChildren();const w=work[c.name]||(work[c.name]={dirty:false});
  const head=el("div",{class:"head"}),[cls,txt]=STATE_TEXT[c.state]||["need",c.state];
  head.append(el("h2",{},c.name),el("span",{class:"dev"},c.source==="robot"?`built-in camera of the robot ${c.device}`:c.device),el("span",{class:"chip "+cls},txt+(c.done_at?" · "+hhmm(c.done_at):"")));
  host.append(head);
  if(c.state==="needs_map"){host.append(el("div",{class:"say"},"This camera has no slot map yet. The agent gives it one (map_camera); you adjust the boxes here afterwards."));
    const t=el("div",{class:"tools"}),skip=el("button",{class:"b",type:"button"},"Skip this camera"),msg=el("span",{class:"dev"});skip.onclick=()=>decide(c.name,"skip",msg);t.append(skip,msg);host.append(t);return}
  const f=c.frame;
  if(f){const k=f.compare.result;host.append(el("div",{class:"say "+(k==="unmoved"?"good":"warn")},
    f.compare.text.charAt(0).toUpperCase()+f.compare.text.slice(1)+(k==="unmoved"?" since its reference picture.":k==="moved"?": the boxes are drawn where the deck is now. Adjust them if they are off, then save.":". You can still adjust the boxes or say they are still right.")))}
  else if(busy[c.name])host.append(el("div",{class:"say"},"Taking a picture…"));
  else host.append(el("div",{class:"say "+(errors[c.name]?"err":"")},errors[c.name]||"No picture yet."));
  const tools=el("div",{class:"tools"}),again=el("button",{class:"b",type:"button"},"Take a new picture"),
    still=el("button",{class:"b main",type:"button"},"Still right"),save=el("button",{class:"b",type:"button"},"Save boxes"),
    undo=el("button",{class:"b",type:"button"},"Undo"),skip=el("button",{class:"b",type:"button"},"Skip this camera"),msg=el("span",{class:"dev"});
  still.disabled=!f||w.dirty;save.disabled=undo.disabled=!w.dirty;again.disabled=!!busy[c.name]||w.dirty;
  tools.append(still,save,undo,again,skip,msg);host.append(tools);
  if(!f){if(!tried[c.name])takeFrame(c.name);again.onclick=()=>takeFrame(c.name);skip.onclick=()=>decide(c.name,"skip",msg);return}
  const [W,H]=f.size;if(!w.slots)w.slots=JSON.parse(JSON.stringify(f.boxes));const slots=w.slots;
  const svg=sv("svg",{class:"work",viewBox:viewbox(W,H,Math.round(Math.max(W,H)*.1)),role:"img","aria-label":`${c.name} with slot boxes`});
  svg.append(sv("image",{href:f.image,x:0,y:0,width:W,height:H}),sv("rect",{x:0,y:0,width:W,height:H,fill:"none",stroke:"var(--axis)","stroke-width":1}));
  const r=Math.max(5,Math.round(W/110)),pt=pointer(svg),parts={};
  const draw=()=>Object.entries(slots).forEach(([k,b])=>{const p=parts[k];p.rect.setAttribute("x",b[0]);p.rect.setAttribute("y",b[1]);p.rect.setAttribute("width",b[2]-b[0]);p.rect.setAttribute("height",b[3]-b[1]);
    p.label.setAttribute("x",Math.max(b[0],0)+6);p.label.setAttribute("y",Math.max(b[1],0)+20);p.handles.forEach((h,i)=>{h.setAttribute("cx",i%3?b[2]:b[0]);h.setAttribute("cy",i<2?b[1]:b[3])})});
  const touched=()=>{w.dirty=true;save.disabled=undo.disabled=false;still.disabled=true;again.disabled=true;msg.textContent="Unsaved. Saving makes the config wait for the person's confirmation.";draw()};
  Object.keys(slots).forEach(k=>{const tips=k.startsWith("tips:"),g=sv("g"),rect=sv("rect",{class:"box"+(tips?" tips":"")}),label=sv("text",{});label.textContent=tips?k.slice(5):"slot "+k;g.append(rect,label);const handles=[];
    rect.addEventListener("pointerdown",ev=>{ev.preventDefault();rect.setPointerCapture(ev.pointerId);const o=pt(ev),b0=slots[k].slice();
      rect.onpointermove=e=>{const q=pt(e);slots[k]=[b0[0]+q[0]-o[0],b0[1]+q[1]-o[1],b0[2]+q[0]-o[0],b0[3]+q[1]-o[1]];touched()};rect.onpointerup=()=>{rect.onpointermove=rect.onpointerup=null}});
    [[0,1],[2,1],[2,3],[0,3]].forEach(([ix,iy])=>{const h=sv("circle",{class:"handle",r});handles.push(h);g.append(h);
      h.addEventListener("pointerdown",ev=>{ev.preventDefault();ev.stopPropagation();h.setPointerCapture(ev.pointerId);
        h.onpointermove=e=>{const q=pt(e),b=slots[k].slice();b[ix]=q[0];b[iy]=q[1];if(b[2]-b[0]>=12&&b[3]-b[1]>=12){slots[k]=b;touched()}};h.onpointerup=()=>{h.onpointermove=h.onpointerup=null}})});
    parts[k]={rect,label,handles};svg.append(g)});
  draw();host.append(svg);
  again.onclick=()=>{work[c.name]={dirty:false};takeFrame(c.name)};
  undo.onclick=()=>{work[c.name]={dirty:false};repaint()};
  still.onclick=()=>decide(c.name,"still-right",msg);skip.onclick=()=>decide(c.name,"skip",msg);
  save.onclick=async()=>{save.disabled=true;msg.textContent="Saving…";const r=await api(`/calibration/${encodeURIComponent(c.name)}/boxes`,{slots});
    if(!r.ok){msg.textContent=`Not saved (${r.status}): ${r.j.error||""}`;save.disabled=false;return}
    work[c.name]={dirty:false};await poll()}}
async function decide(name,what,msg){msg.textContent="…";const r=await api(`/calibration/${encodeURIComponent(name)}/${what}`);
  if(!r.ok){msg.textContent=`Not done (${r.status}): ${r.j.error||""}`;return}work[name]={dirty:false};await poll()}

// -- plate calibration ---------------------------------------------------------------------------
function homography(src,dst){const A=[],b=[];
  for(let i=0;i<4;i++){const[x,y]=src[i],[u,v]=dst[i];A.push([x,y,1,0,0,0,-u*x,-u*y]);b.push(u);A.push([0,0,0,x,y,1,-v*x,-v*y]);b.push(v)}
  const n=8;for(let i=0;i<n;i++){let m=i;for(let r=i+1;r<n;r++)if(Math.abs(A[r][i])>Math.abs(A[m][i]))m=r;
    [A[i],A[m]]=[A[m],A[i]];[b[i],b[m]]=[b[m],b[i]];if(Math.abs(A[i][i])<1e-12)return null;
    for(let r=i+1;r<n;r++){const f=A[r][i]/A[i][i];for(let c=i;c<n;c++)A[r][c]-=f*A[i][c];b[r]-=f*b[i]}}
  const h=Array(n);for(let i=n-1;i>=0;i--){let s=b[i];for(let c=i+1;c<n;c++)s-=A[i][c]*h[c];h[i]=s/A[i][i]}return h}
const through=(h,x,y)=>{const w=h[6]*x+h[7]*y+1;return[(h[0]*x+h[1]*y+h[2])/w,(h[3]*x+h[4]*y+h[5])/w]};
// The grid of well centres from the four corner points (A1, A<cols>, <last row>1, <last row><cols>); row by row.
function wellGrid(pts,rows,cols){const h=homography([[0,0],[cols-1,0],[0,rows-1],[cols-1,rows-1]],pts);if(!h)return null;
  const out=[];for(let r=0;r<rows;r++)for(let c=0;c<cols;c++)out.push(through(h,c,r));
  let pitch=Infinity;for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){const a=out[r*cols+c];
    if(c+1<cols)pitch=Math.min(pitch,Math.hypot(a[0]-out[r*cols+c+1][0],a[1]-out[r*cols+c+1][1]));
    if(r+1<rows)pitch=Math.min(pitch,Math.hypot(a[0]-out[(r+1)*cols+c][0],a[1]-out[(r+1)*cols+c][1]))}
  return{centres:out,pitch}}
function plateCard(host,p,cam){
  host.replaceChildren();const w=pwork[p.labware]||(pwork[p.labware]={dirty:false,pts:null});
  const head=el("div",{class:"head"}),done=p.state==="calibrated";
  head.append(el("h2",{},"Plate "+p.labware),el("span",{class:"dev"},`slot ${p.slot} · ${p.type} · camera ${p.camera}`),
    el("span",{class:"chip "+(done?"done":"need")},done?"calibrated"+(p.calibrated_at?" · "+hhmm(p.calibrated_at):""):"waiting for you"));host.append(head);
  host.append(el("div",{class:"say"},`The agent asks: ${p.why}`+(p.plan_step?` (plan step ${p.plan_step})`:"")));
  if(p.note)host.append(el("div",{class:"say warn"},p.note));
  const f=cam&&cam.frame;
  if(!f){host.append(el("div",{class:"say"+(errors[p.camera]?" err":"")},busy[p.camera]?"Taking a picture…":errors[p.camera]||"No picture yet."));if(cam&&!tried[p.camera])takeFrame(p.camera);return}
  if(w.pts===null)w.pts=(p.corners_in_frame||p.earlier_corners||[]).map(q=>q.slice());
  const pts=w.pts,box=f.boxes[p.slot]||[0,0,f.size[0],f.size[1]],bw=box[2]-box[0],bh=box[3]-box[1],pad=Math.max(bw,bh)*.12;
  const hint=el("div",{class:"hint"}),tools=el("div",{class:"tools"}),msg=el("span",{class:"dev"}),
    save=el("button",{class:"b main",type:"button"},"Save plate calibration"),over=el("button",{class:"b",type:"button"},"Start over"),
    still=el("button",{class:"b main",type:"button"},"Earlier corners are still right");
  const earlierOffer=p.earlier&&!w.dirty&&!done;still.hidden=!earlierOffer;tools.append(save,still,over,msg);host.append(hint,tools);
  const svg=sv("svg",{class:"work",viewBox:`${box[0]-pad} ${box[1]-pad} ${bw+2*pad} ${bh+2*pad}`,role:"img","aria-label":`${p.labware}: click its corner wells`});
  svg.append(sv("image",{href:f.image,x:0,y:0,width:f.size[0],height:f.size[1]}),sv("rect",{class:"slotbox",x:box[0],y:box[1],width:bw,height:bh}));
  const layer=sv("g");svg.append(layer);host.append(svg);
  const pt=pointer(svg),hr=Math.max(bw,bh)/90;
  const draw=()=>{layer.replaceChildren();
    if(pts.length===4){const g=wellGrid(pts,p.rows,p.cols);if(g){const rr=g.pitch*p.fraction/2;g.centres.forEach(([x,y])=>layer.append(sv("circle",{class:"well",cx:x,cy:y,r:rr})))}}
    pts.forEach((q,i)=>{const c=sv("circle",{class:"pt"+(i===0?" first":""),cx:q[0],cy:q[1],r:hr});
      c.addEventListener("pointerdown",ev=>{ev.preventDefault();ev.stopPropagation();c.setPointerCapture(ev.pointerId);
        c.onpointermove=e=>{const n=pt(e);pts[i]=[n[0],n[1]];w.dirty=true;refresh();c.setAttribute("cx",n[0]);c.setAttribute("cy",n[1]);redrawGrid()};c.onpointerup=()=>{c.onpointermove=c.onpointerup=null;draw()}});
      const t=sv("text",{x:q[0]+hr*1.5,y:q[1]-hr*1.5,"font-size":hr*3.2});t.textContent=p.wells[i];t.style.fontSize=(hr*3.2)+"px";layer.append(c,t)})};
  const redrawGrid=()=>{layer.querySelectorAll(".well").forEach(n=>n.remove());
    if(pts.length===4){const g=wellGrid(pts,p.rows,p.cols);if(g){const rr=g.pitch*p.fraction/2;g.centres.forEach(([x,y])=>layer.insertBefore(sv("circle",{class:"well",cx:x,cy:y,r:rr}),layer.firstChild))}}};
  const refresh=()=>{hint.textContent=pts.length<4?`Click well ${p.wells[pts.length]} (${pts.length+1} of 4): ${p.wells.join(", ")} in this order`:
      "Drag a point if the grid is off the wells; the grid follows. Save when it sits on them.";
    save.disabled=!(pts.length===4&&(w.dirty||!done));over.disabled=!pts.length;still.hidden=!(p.earlier&&!w.dirty&&!done&&pts.length===4)};
  svg.addEventListener("pointerdown",ev=>{if(pts.length>=4)return;ev.preventDefault();const q=pt(ev);pts.push([q[0],q[1]]);w.dirty=true;draw();refresh()});
  over.onclick=()=>{w.pts=[];w.dirty=true;pwork[p.labware].fresh=true;plateCard(host,{...p,earlier:false},cam)};
  save.onclick=async()=>{save.disabled=true;msg.textContent="Saving…";const r=await api(`/calibration/plate/${encodeURIComponent(p.labware)}/save`,{corners:pts});
    if(!r.ok){msg.textContent=`Not saved (${r.status}): ${r.j.error||""}`;save.disabled=false;return}pwork[p.labware]={dirty:false,pts:null};await poll()};
  still.onclick=async()=>{still.disabled=true;const r=await api(`/calibration/plate/${encodeURIComponent(p.labware)}/save`,{earlier:true});
    if(!r.ok){msg.textContent=`Not done (${r.status}): ${r.j.error||""}`;still.disabled=false;return}pwork[p.labware]={dirty:false,pts:null};await poll()};
  draw();refresh()}

// -- the page ------------------------------------------------------------------------------------
function banner(){const b=document.getElementById("banner"),left=ST.cameras.filter(c=>c.state==="pending"),waiting=ST.cameras.filter(c=>c.state==="needs_map");
  b.replaceChildren();b.className="banner";
  if(!ST.active){b.append(el("div",{},"Nothing to calibrate: this server has no real robot with cameras."));return}
  if(left.length){b.append(el("div",{},`Calibrate ${left.map(c=>c.name).join(", ")} before the agent loads the deck.`),
    el("div",{class:"more"},"For each camera: say the boxes are still right, adjust them and save, or skip the camera (it then covers nothing; the agent can still take photos with it)."))}
  else if(waiting.length){b.append(el("div",{},`Waiting for the agent: ${waiting.map(c=>c.name).join(", ")} still needs a slot map (map_camera).`))}
  else{b.className="banner ok";b.append(el("div",{},"Every camera is done for this session. The agent can load the deck."),
    (()=>{const d=el("div",{class:"more"});d.append(el("a",{href:"/?page=run"},"Back to the run page"));return d})())}
  if(ST.stale&&ST.auto_verify)b.append(el("div",{class:"more"},"Auto-verify is off until the agent runs physical_world_context again."))}
function sync(map,host,items,key,build){const keep=new Set(items.map(key));
  Object.keys(map).forEach(k=>{if(!keep.has(k)){map[k].el.remove();delete map[k]}});
  items.forEach(it=>{const k=key(it);if(!map[k]){map[k]={el:el("section",{class:"card"}),sig:null};host.append(map[k].el);map[k].el.style.marginBottom="16px"}})}
function repaint(){if(!ST)return;
  banner();const camHost=document.getElementById("cams"),plHost=document.getElementById("plates");
  sync(cards,camHost,ST.cameras,c=>c.name);
  ST.cameras.forEach(c=>{const card=cards[c.name],sig=JSON.stringify([c,!!busy[c.name],errors[c.name]]);
    if(card.sig===sig||(work[c.name]||{}).dirty)return;card.sig=sig;cameraCard(card.el,c)});
  sync(plateCards,plHost,ST.plates,p=>p.labware);
  ST.plates.forEach(p=>{const card=plateCards[p.labware],cam=ST.cameras.find(c=>c.name===p.camera),sig=JSON.stringify([p,cam&&cam.frame&&cam.frame.t,!!busy[p.camera],errors[p.camera]]);
    if(card.sig===sig||(pwork[p.labware]||{}).dirty)return;card.sig=sig;plateCard(card.el,p,cam)})}
async function poll(){try{const r=await fetch("/calibration/state",{cache:"no-store"});if(r.ok){ST=await r.json();repaint()}}catch(e){}}
poll();setInterval(poll,2000);
"""

PAGE = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="ahc-token" content="__AHC_TOKEN__"><meta name="ahc-view" content="__AHC_VIEW__">
<title>AHC calibration · __AHC_FOLDER__</title>
<style>""" + CSS + "__AHC_STOPBAR_CSS__" + r"""</style></head><body>
__AHC_STOPBAR_HTML__""" + BODY + "<script>" + JS + "</script></body></html>")


def render_page(ws: Any, token: str) -> str:
  """The calibration page: the control dashboard's own, with the stop bar (never served view-only)."""
  page = (PAGE.replace("__AHC_STOPBAR_CSS__", stopbar.CSS).replace("__AHC_STOPBAR_HTML__", stopbar.HTML)
          .replace("__AHC_STOPBAR_JS__", stopbar.JS))
  return stopbar.fill(page, ws, token, False)
