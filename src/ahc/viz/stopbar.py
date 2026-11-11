"""The emergency-stop bar every AHC dashboard page carries: its CSS, markup and script, shared by the run page and the
calibration page so the stop is always one click away. The script needs `el` (a tiny element builder), `TOKEN` and
`VIEW` (the page's release token and whether it is the view-only page) defined before it."""

from __future__ import annotations

import html

from ahc.workspace import Workspace

CSS = r""".stopbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;padding:10px 24px;
background:var(--page);border-bottom:1px solid var(--border);flex-wrap:wrap}
.stopbtn{float:none;font-size:15px;font-weight:700;letter-spacing:.04em;padding:10px 18px;border-radius:8px;
background:var(--critical);color:#fff;border:2px solid var(--critical);cursor:pointer}
.stopbtn:hover{filter:brightness(1.08)}.stopbtn::before{content:"■ "}
.stopbar .state{color:var(--ink2);font-size:13px}
.stopbar.engaged{background:var(--critical);color:#fff}.stopbar.engaged .state{color:#fff;font-weight:600;font-size:15px}
.stopbar.engaged .stopbtn{display:none}
.releasebtn{float:none;font-size:13px;padding:6px 12px;border-radius:8px;background:#fff;color:var(--critical);
border:2px solid #fff;cursor:pointer;display:none}.stopbar.engaged .releasebtn{display:inline-block}
.stopbar .folder{margin-left:auto;color:var(--ink2);font-size:12px;overflow-wrap:anywhere}.stopbar.engaged .folder{color:#fff}
.stopbar .servers{flex-basis:100%;font-size:12px}.stopbar .servers:empty{display:none}
.stopbar .servers div{padding:1px 0;overflow-wrap:anywhere}.stopbar.engaged .servers{color:#fff}
.stopbar .limit{flex-basis:100%;font-size:12px;color:var(--ink2)}.stopbar .limit:empty{display:none}
.stopbar.engaged .limit{color:#fff}
.stopbar .note{flex-basis:100%;font-weight:700;color:var(--critical)}.stopbar .note:empty{display:none}
.stopbar.engaged .note{color:#fff;text-decoration:underline}
"""

HTML = r"""<div class="stopbar" id="stopbar">__AHC_STOPBTN__
<span class="state" id="stopstate">__AHC_IDLE__</span>
__AHC_RELEASEBTN__
<span class="folder" title="The task folder this page shows">Task folder <code>__AHC_FOLDER__</code></span>
<span class="limit" id="stoplimit"></span>
<span class="note" id="stopnote"></span><div class="servers" id="stopservers"></div></div>
"""

JS = r"""const ELSEWHERE="Use the robot's own stop or its power switch, or ahc-estop in the task folder.";
// A press that did not arrive is said at once and stays until a stop shows engaged; never silent.
function note(text){const n=document.getElementById("stopnote");n.textContent=text;clearTimeout(note.t);
  if(text&&!text.startsWith("STOP NOT SENT"))note.t=setTimeout(()=>{n.textContent=""},20000)}
async function control(path,body){const stop=path==="/estop";
  const headers={"X-AHC-Control":"1","Content-Type":"application/json"};if(!stop)headers["X-AHC-Token"]=TOKEN;
  let r;try{r=await fetch(path,{method:"POST",headers,body:JSON.stringify(body||{})})}
  catch(err){note(stop?"STOP NOT SENT: the dashboard did not answer. "+ELSEWHERE:"Release not sent: the dashboard did not answer.");return false}
  if(r.ok){note("");return true}
  let why="";try{why=(await r.json()).error||""}catch(err){}
  note(stop?`STOP NOT SENT (${r.status} ${why}). `+ELSEWHERE:`Not released (${r.status}): ${why}`);return false}
const stopBtn=document.getElementById("stopbtn"),releaseBtn=document.getElementById("releasebtn");  // absent on the view-only page
if(stopBtn)stopBtn.onclick=async()=>{await control("/estop",{reason:"pressed on the dashboard"});pollStop()};
if(releaseBtn)releaseBtn.onclick=async()=>{
  if(!confirm("Release the emergency stop?\n\nOnly when the deck is safe. The agent must check the deck again before anything moves."))return;
  await control("/estop/release",{confirm:true});pollStop()};
const IDLE=VIEW?"View only: the stop and its release are on the person's own page.":"Stops the hardware commands of this task folder at once; only a person releases it.";
async function pollStop(){const bar=document.getElementById("stopbar"),state=document.getElementById("stopstate"),list=document.getElementById("stopservers");
  try{const r=await fetch("/estop",{cache:"no-store"});const e=await r.json();
  document.getElementById("stoplimit").textContent=e.note||"";
  if(e.engaged){bar.className="stopbar engaged";
    let text=`■ EMERGENCY STOP ENGAGED · pressed ${(e.at||"").replace("T"," ")} (${e.by})`;
    if(e.unreadable)text+=` · the stop file is not one AHC wrote (${e.unreadable}), so it counts as pressed`;
    // One line per AHC server that took the stop (its own acknowledgement), then what the person may rely on.
    text+=` · ${e.verdict?e.verdict.text:"no verdict"}`;
    state.textContent=text+" · nothing runs until a person releases it";
    list.replaceChildren(...(e.servers||[]).map(s=>el("div",{},s.line)));
    if(document.getElementById("stopnote").textContent.startsWith("STOP NOT SENT"))note("")}
  else{bar.className="stopbar";list.replaceChildren();state.textContent=e.released_at?`Released ${(e.released_at||"").replace("T"," ")} by ${e.released_by}; the deck must be checked again before anything moves.`:IDLE}}
  catch(err){state.textContent="The dashboard does not answer"+(VIEW?".":": this button cannot stop anything now. "+ELSEWHERE)}
  clearTimeout(pollStop.t);pollStop.t=setTimeout(pollStop,500)}
pollStop();
"""

STOP_BUTTON = ('<button class="stopbtn" id="stopbtn" title="Stops every command of this task folder\'s server at once">'
               'EMERGENCY STOP</button>')
RELEASE_BUTTON = '<button class="releasebtn" id="releasebtn">Release…</button>'
IDLE_TEXT = "Stops the hardware commands of this task folder at once; only a person releases it."
VIEW_ONLY_TEXT = "View only: the stop and its release are on the person's own page."


def fill(page: str, ws: Workspace, token: str, view_only: bool) -> str:
  """The page's placeholders: the stop and release buttons (not on the view-only page, which has no release token
  either), the folder, the token and the view flag."""
  return (page.replace("__AHC_STOPBTN__", "" if view_only else STOP_BUTTON)
          .replace("__AHC_RELEASEBTN__", "" if view_only else RELEASE_BUTTON)
          .replace("__AHC_IDLE__", VIEW_ONLY_TEXT if view_only else IDLE_TEXT)
          .replace("__AHC_VIEW__", "1" if view_only else "0")
          .replace("__AHC_FOLDER__", html.escape(str(ws.root)))
          .replace("__AHC_TOKEN__", "" if view_only else token))
