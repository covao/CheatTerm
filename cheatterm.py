"""CheatTerm -- Browser-based terminal with WebSocket + PTY.

Architecture:
  - Tornado WebSocket server
  - PTY (pseudo-terminal) per connection via pty.fork()
  - xterm.js frontend (loaded from CDN) with multi-tab support
  - File manager with upload/download/edit capabilities
  - Full TTY support: SSH, vim, top, htop, colors, etc.
"""

import fcntl
import os
import pty
import signal
import struct
import termios
import json
import argparse
import datetime
import mimetypes
import zipfile
import io
import tornado.ioloop
import tornado.web
import tornado.websocket

try:
    import yaml
except ImportError:
    yaml = None

SHELL = os.environ.get("SHELL", "/bin/bash")
CHEAT_JSON = "null"
FILE_ROOT = os.path.expanduser("~")


def safe_path(requested, base=None):
    """Resolve a path and ensure it stays within base. Returns None if invalid."""
    if base is None:
        base = FILE_ROOT
    base = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base, requested))
    if not full.startswith(base + os.sep) and full != base:
        return None
    return full


class TerminalWebSocket(tornado.websocket.WebSocketHandler):
    """One WebSocket = one PTY + child bash process."""

    def open(self):
        self.child_pid, self.fd = pty.fork()
        if self.child_pid == 0:
            os.execvpe(SHELL, [SHELL], os.environ)
        else:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._set_winsize(80, 24)
            self.io_loop = tornado.ioloop.IOLoop.current()
            self.io_loop.add_handler(
                self.fd, self._on_pty_read, tornado.ioloop.IOLoop.READ)

    def _set_winsize(self, cols, rows):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            os.kill(self.child_pid, signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def _on_pty_read(self, fd, events):
        try:
            data = os.read(fd, 65536)
            if data:
                self.write_message(data, binary=True)
            else:
                self._cleanup()
        except OSError:
            self._cleanup()

    def on_message(self, message):
        if isinstance(message, bytes):
            try:
                os.write(self.fd, message)
            except OSError:
                self._cleanup()
        else:
            try:
                msg = json.loads(message)
                if msg.get("type") == "resize":
                    self._set_winsize(msg["cols"], msg["rows"])
            except (json.JSONDecodeError, KeyError):
                pass

    def on_close(self):
        self._cleanup()

    def _cleanup(self):
        try:
            self.io_loop.remove_handler(self.fd)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.child_pid, signal.SIGKILL)
            os.waitpid(self.child_pid, os.WNOHANG)
        except (ProcessLookupError, ChildProcessError, OSError):
            pass

    def check_origin(self, origin):
        return True


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(HTML.replace("__CHEAT_JSON_SLOT__", CHEAT_JSON))


# ---- File Manager API Handlers ----

class FileListHandler(tornado.web.RequestHandler):
    """List directory contents."""
    def get(self):
        rel = self.get_argument("path", ".")
        full = safe_path(rel)
        # Fallback to FILE_ROOT for invalid or non-directory paths
        if full is None or not os.path.isdir(full):
            full = os.path.realpath(FILE_ROOT)
        items = []
        try:
            for name in sorted(os.listdir(full)):
                fp = os.path.join(full, name)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                is_dir = os.path.isdir(fp)
                items.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": st.st_size if not is_dir else 0,
                    "mtime": datetime.datetime.fromtimestamp(
                        st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "is_link": os.path.islink(fp),
                })
        except PermissionError:
            self.set_status(403)
            self.write({"error": "permission denied"})
            return
        self.write({"path": full, "items": items})


class FileReadHandler(tornado.web.RequestHandler):
    """Read text file content."""
    MAX_TEXT_SIZE = 1 * 1024 * 1024  # 1 MB

    def get(self):
        rel = self.get_argument("path", "")
        full = safe_path(rel)
        if full is None or not os.path.isfile(full):
            self.set_status(403 if full is None else 404)
            self.write({"error": "invalid path"})
            return
        size = os.path.getsize(full)
        # Large file: return empty with notice
        if size > self.MAX_TEXT_SIZE:
            self.write({"path": rel, "content": "",
                        "notice": f"File too large ({size:,} bytes). Max 1 MB for text view.",
                        "editable": False})
            return
        # Binary check
        try:
            with open(full, "rb") as f:
                chunk = f.read(8192)
            if b"\x00" in chunk:
                self.write({"path": rel, "content": "",
                            "notice": "Binary file. Cannot display as text.",
                            "editable": False})
                return
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.write({"path": rel, "content": content, "editable": True})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class FileWriteHandler(tornado.web.RequestHandler):
    """Write text file content."""
    def post(self):
        data = json.loads(self.request.body)
        rel = data.get("path", "")
        content = data.get("content", "")
        full = safe_path(rel)
        if full is None:
            self.set_status(403)
            self.write({"error": "invalid path"})
            return
        try:
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            self.write({"ok": True})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class FileDownloadHandler(tornado.web.RequestHandler):
    """Download a single file."""
    def get(self):
        rel = self.get_argument("path", "")
        full = safe_path(rel)
        if full is None or not os.path.isfile(full):
            self.set_status(403 if full is None else 404)
            self.write({"error": "invalid path"})
            return
        name = os.path.basename(full)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.set_header("Content-Type", mime)
        self.set_header("Content-Disposition", f'attachment; filename="{name}"')
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.write(chunk)
        self.finish()


class FileZipHandler(tornado.web.RequestHandler):
    """Zip a directory and download."""
    def get(self):
        rel = self.get_argument("path", "")
        full = safe_path(rel)
        if full is None or not os.path.isdir(full):
            self.set_status(403 if full is None else 404)
            self.write({"error": "invalid path"})
            return
        name = os.path.basename(full) or "root"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(full):
                for fn in files:
                    fp = os.path.join(root, fn)
                    arcname = os.path.relpath(fp, full)
                    try:
                        zf.write(fp, arcname)
                    except (PermissionError, OSError):
                        pass
        buf.seek(0)
        self.set_header("Content-Type", "application/zip")
        self.set_header("Content-Disposition",
                        f'attachment; filename="{name}.zip"')
        self.write(buf.read())
        self.finish()


class FileUploadHandler(tornado.web.RequestHandler):
    """Upload files via multipart form."""
    def post(self):
        rel = self.get_argument("path", ".")
        full = safe_path(rel)
        if full is None or not os.path.isdir(full):
            self.set_status(403)
            self.write({"error": "invalid path"})
            return
        uploaded = []
        for field_name, file_list in self.request.files.items():
            for finfo in file_list:
                dest = os.path.join(full, os.path.basename(finfo["filename"]))
                with open(dest, "wb") as f:
                    f.write(finfo["body"])
                uploaded.append(finfo["filename"])
        self.write({"ok": True, "files": uploaded})


class FileMkdirHandler(tornado.web.RequestHandler):
    """Create a directory."""
    def post(self):
        data = json.loads(self.request.body)
        rel = data.get("path", "")
        full = safe_path(rel)
        if full is None:
            self.set_status(403)
            self.write({"error": "invalid path"})
            return
        try:
            os.makedirs(full, exist_ok=True)
            self.write({"ok": True})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class FileDeleteHandler(tornado.web.RequestHandler):
    """Delete a file or directory."""
    def post(self):
        import shutil
        data = json.loads(self.request.body)
        rel = data.get("path", "")
        full = safe_path(rel)
        if full is None or full == os.path.realpath(FILE_ROOT):
            self.set_status(403)
            self.write({"error": "invalid path"})
            return
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            self.write({"ok": True})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class FileRenameHandler(tornado.web.RequestHandler):
    """Rename / move a file or directory."""
    def post(self):
        data = json.loads(self.request.body)
        old_rel = data.get("old_path", "")
        new_rel = data.get("new_path", "")
        old_full = safe_path(old_rel)
        new_full = safe_path(new_rel)
        if old_full is None or new_full is None:
            self.set_status(403)
            self.write({"error": "invalid path"})
            return
        try:
            os.rename(old_full, new_full)
            self.write({"ok": True})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


# ---- HTML ----

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CheatTerm</title>
<link rel="stylesheet"
  href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<style>
:root {
  --base:#1e1e2e;--mantle:#181825;--crust:#11111b;
  --surface0:#313244;--surface1:#45475a;
  --text:#cdd6f4;--subtext:#a6adc8;
  --blue:#89b4fa;--green:#a6e3a1;--pink:#f5c2e7;
  --red:#f38ba8;--yellow:#f9e2af;--overlay:#6c7086;
  --peach:#fab387;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:var(--base);font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden}

/* Tab Bar */
#tab-bar{display:flex;align-items:center;background:var(--crust);height:38px;padding:0 4px;gap:2px;border-bottom:1px solid var(--surface0);user-select:none}
#tab-list{display:flex;align-items:center;gap:2px;flex:1;overflow-x:auto;scrollbar-width:none}
#tab-list::-webkit-scrollbar{display:none}
.tab{display:flex;align-items:center;gap:6px;padding:0 12px;height:30px;background:var(--mantle);color:var(--subtext);border-radius:6px 6px 0 0;font-size:12px;font-weight:500;cursor:pointer;white-space:nowrap;flex-shrink:0;transition:background .15s,color .15s;border:1px solid transparent;border-bottom:none}
.tab:hover{background:var(--surface0);color:var(--text)}
.tab.active{background:var(--base);color:var(--text);border-color:var(--surface0)}
.tab .dot{width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0}
.tab.dead .dot{background:var(--red)}
.tab.fm .dot{background:var(--peach)}
.tab .close{display:flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:4px;font-size:14px;line-height:1;color:var(--overlay);opacity:0;transition:opacity .15s,background .15s,color .15s}
.tab:hover .close{opacity:1}
.tab .close:hover{background:var(--red);color:var(--crust)}

.tb-btn{display:flex;align-items:center;justify-content:center;width:30px;height:30px;background:none;border:none;color:var(--overlay);font-size:15px;cursor:pointer;border-radius:6px;flex-shrink:0;transition:background .15s,color .15s}
.tb-btn:hover{background:var(--surface0);color:var(--text)}
.tb-btn.active{color:var(--blue)}
#btn-new{font-size:18px}

/* Main layout */
#main{display:flex;height:calc(100% - 38px);width:100%}
#terminals{position:relative;flex:1;min-width:0}
.term-container,.fm-container{position:absolute;inset:0;display:none;overflow:hidden}
.term-container.active,.fm-container.active{display:flex;flex-direction:column}

/* Cheat Panel */
#btn-cheat{display:none}
#cheat-panel{width:280px;flex-shrink:0;background:var(--mantle);border-left:1px solid var(--surface0);display:none;flex-direction:column;overflow:hidden}
#cheat-panel.open{display:flex}
#cheat-header{padding:10px 12px 8px;font-size:13px;font-weight:600;color:var(--text);border-bottom:1px solid var(--surface0);display:flex;align-items:center;gap:6px}
#cheat-search{margin:8px;padding:6px 10px;background:var(--surface0);border:1px solid var(--surface1);border-radius:6px;color:var(--text);font-size:12px;font-family:inherit;outline:none}
#cheat-search::placeholder{color:var(--overlay)}
#cheat-search:focus{border-color:var(--blue)}
#cheat-body{flex:1;overflow-y:auto;padding:4px 0;scrollbar-width:thin;scrollbar-color:var(--surface1) transparent}
.cheat-group-name{padding:8px 12px 4px;font-size:11px;font-weight:600;color:var(--overlay);text-transform:uppercase;letter-spacing:.5px}
.cheat-item{display:flex;flex-direction:column;padding:6px 12px;cursor:pointer;transition:background .12s}
.cheat-item:hover{background:var(--surface0)}
.cheat-item .cheat-label{font-size:12px;color:var(--text);font-weight:500}
.cheat-item .cheat-cmd{font-size:11px;color:var(--overlay);font-family:'JetBrains Mono','Fira Code','Courier New',monospace;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* File Manager */
.fm-toolbar{display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--mantle);border-bottom:1px solid var(--surface0);flex-shrink:0}
.fm-path{flex:1;padding:5px 10px;background:var(--surface0);border:1px solid var(--surface1);border-radius:5px;color:var(--text);font-size:12px;font-family:'JetBrains Mono',monospace;outline:none;min-width:0}
.fm-path:focus{border-color:var(--blue)}
.fm-btn{padding:4px 10px;background:var(--surface0);border:1px solid var(--surface1);border-radius:5px;color:var(--text);font-size:12px;cursor:pointer;white-space:nowrap;transition:background .12s}
.fm-btn:hover{background:var(--surface1)}
.fm-body{flex:1;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--surface1) transparent}
.fm-drop-overlay{position:absolute;inset:0;background:rgba(137,180,250,.12);border:2px dashed var(--blue);display:none;align-items:center;justify-content:center;font-size:18px;color:var(--blue);z-index:10;pointer-events:none}
.fm-drop-overlay.show{display:flex}

/* File list */
.fm-table{width:100%;border-collapse:collapse;font-size:12px}
.fm-table th{position:sticky;top:0;background:var(--mantle);padding:6px 10px;text-align:left;font-weight:600;color:var(--overlay);font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--surface0);cursor:pointer;user-select:none}
.fm-table th:hover{color:var(--text)}
.fm-table td{padding:5px 10px;border-bottom:1px solid var(--surface0);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fm-table tr:hover td{background:var(--surface0)}
.fm-table tr.selected td{background:rgba(137,180,250,.15)}
.fm-name{display:flex;align-items:center;gap:6px;cursor:pointer}
.fm-name:hover{color:var(--blue)}
.fm-icon{flex-shrink:0;font-size:14px}
.fm-size{color:var(--subtext);text-align:right;font-family:'JetBrains Mono',monospace;font-size:11px}
.fm-mtime{color:var(--subtext);font-size:11px}
.fm-actions{display:flex;gap:4px}
.fm-act{padding:2px 6px;border-radius:3px;border:none;background:none;color:var(--overlay);cursor:pointer;font-size:12px;transition:background .1s,color .1s}
.fm-act:hover{background:var(--surface1);color:var(--text)}
.fm-act.del:hover{background:var(--red);color:var(--crust)}

/* Editor pane */
.fm-editor{display:none;flex-direction:column;flex-shrink:0;border-top:1px solid var(--surface0);height:45%}
.fm-editor.open{display:flex}
.fm-editor-bar{display:flex;align-items:center;gap:6px;padding:4px 10px;background:var(--mantle);border-bottom:1px solid var(--surface0)}
.fm-editor-name{flex:1;font-size:12px;color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis}
.fm-editor-name.dirty::after{content:" *";color:var(--yellow)}
.fm-editor textarea{flex:1;background:var(--base);color:var(--text);border:none;padding:10px;font:13px/1.5 'JetBrains Mono','Fira Code','Courier New',monospace;resize:none;outline:none;tab-size:4}
.fm-editor textarea[readonly]{color:var(--subtext);cursor:default}
.fm-editor-status{display:flex;align-items:center;gap:12px;padding:3px 10px;background:var(--crust);font-size:11px;color:var(--overlay);font-family:'JetBrains Mono',monospace;border-top:1px solid var(--surface0)}
</style>
</head>
<body>
<div id="tab-bar">
  <div id="tab-list"></div>
  <button class="tb-btn" id="btn-new" title="New terminal">+</button>
  <button class="tb-btn" id="btn-fm" title="File manager">&#128193;</button>
  <button class="tb-btn" id="btn-cheat" title="Cheat sheet">&#9776;</button>
</div>
<div id="main">
  <div id="terminals"></div>
  <div id="cheat-panel">
    <div id="cheat-header"><span>&#9776;</span><span id="cheat-title">Commands</span></div>
    <input id="cheat-search" type="text" placeholder="Search commands...">
    <div id="cheat-body"></div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/lib/addon-web-links.min.js"></script>
<script>
(function(){

const THEME={background:'#1e1e2e',foreground:'#cdd6f4',cursor:'#f5c2e7',cursorAccent:'#1e1e2e',black:'#45475a',red:'#f38ba8',green:'#a6e3a1',yellow:'#f9e2af',blue:'#89b4fa',magenta:'#f5c2e7',cyan:'#94e2d5',white:'#bac2de',brightBlack:'#585b70',brightRed:'#f38ba8',brightGreen:'#a6e3a1',brightYellow:'#f9e2af',brightBlue:'#89b4fa',brightMagenta:'#f5c2e7',brightCyan:'#94e2d5',brightWhite:'#a6adc8',selectionBackground:'rgba(137,180,250,0.3)'};

const tabListEl=document.getElementById('tab-list');
const terminalsEl=document.getElementById('terminals');
const btnNew=document.getElementById('btn-new');
const btnFm=document.getElementById('btn-fm');

let tabs=[];
let activeId=null;
let nextId=1;
let nextTermNum=1;
let nextFmNum=1;

/* ========== Generic Tab helpers ========== */

function makeTabEl(label, cls){
  const tabEl=document.createElement('div');
  tabEl.className='tab'+(cls?' '+cls:'');
  tabEl.innerHTML='<span class="dot"></span><span class="label"></span><span class="close">&times;</span>';
  tabEl.querySelector('.label').textContent=label;
  tabListEl.appendChild(tabEl);
  return tabEl;
}

function switchTab(id){
  activeId=id;
  const active=tabs.find(t=>t.id===id);
  tabs.forEach(t=>{
    const a=t.id===id;
    t.el.classList.toggle('active',a);
    t.tabEl.classList.toggle('active',a);
    if(a && t.type==='terminal'){
      requestAnimationFrame(()=>{
        t.fitAddon.fit();
        t.term.focus();
        if(t.ws&&t.ws.readyState===WebSocket.OPEN)
          t.ws.send(JSON.stringify({type:'resize',cols:t.term.cols,rows:t.term.rows}));
      });
    }
  });
  // Disable cheat button on file manager tabs
  const bc=document.getElementById('btn-cheat');
  if(bc){
    const isFm=active&&active.type==='filemanager';
    bc.disabled=isFm;
    bc.style.opacity=isFm?'0.3':'';
    bc.style.pointerEvents=isFm?'none':'';
  }
}

function closeTab(id){
  const idx=tabs.findIndex(t=>t.id===id);
  if(idx===-1)return;
  const tab=tabs[idx];
  if(tab.type==='terminal'){
    if(tab.ws&&tab.ws.readyState===WebSocket.OPEN)tab.ws.close();
    tab.term.dispose();
  }
  tab.el.remove();
  tab.tabEl.remove();
  tabs.splice(idx,1);
  if(tabs.length===0){createTermTab();}
  else if(activeId===id){switchTab(tabs[Math.min(idx,tabs.length-1)].id);}
}

/* ========== Terminal Tab ========== */

function createTermTab(opts){
  opts=opts||{};
  const id=nextId++;
  const num=nextTermNum++;
  const el=document.createElement('div');
  el.className='term-container';
  terminalsEl.appendChild(el);

  const term=new Terminal({cursorBlink:true,fontSize:14,fontFamily:"'JetBrains Mono','Fira Code','Courier New',monospace",theme:THEME,allowProposedApi:true});
  const fitAddon=new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(new WebLinksAddon.WebLinksAddon((ev,uri)=>{window.open(uri,'_blank','noopener')}));
  term.open(el);

  const tabEl=makeTabEl('Terminal #'+num,'');
  const tab={id,type:'terminal',term,fitAddon,ws:null,el,tabEl,alive:true};
  tabs.push(tab);

  tabEl.addEventListener('click',e=>{if(!e.target.classList.contains('close'))switchTab(id)});
  tabEl.querySelector('.close').addEventListener('click',e=>{e.stopPropagation();closeTab(id)});

  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.binaryType='arraybuffer';
  tab.ws=ws;

  ws.onopen=()=>{
    fitAddon.fit();
    ws.send(JSON.stringify({type:'resize',cols:term.cols,rows:term.rows}));
    if(activeId===id)term.focus();
    if(opts.displayMsg)term.write('\x1b[36m'+opts.displayMsg+'\x1b[0m\r\n');
    if(opts.startCmd)setTimeout(()=>{if(ws.readyState===WebSocket.OPEN)ws.send(new TextEncoder().encode(opts.startCmd+'\n'))},300);
  };
  ws.onmessage=ev=>{if(ev.data instanceof ArrayBuffer)term.write(new Uint8Array(ev.data));else term.write(ev.data)};
  ws.onclose=()=>{tab.alive=false;tabEl.classList.add('dead');term.write('\r\n\x1b[31m[Process exited]\x1b[0m\r\n')};

  term.onData(d=>{if(ws.readyState===WebSocket.OPEN)ws.send(new TextEncoder().encode(d))});
  term.onBinary(d=>{if(ws.readyState===WebSocket.OPEN){const b=new Uint8Array(d.length);for(let i=0;i<d.length;i++)b[i]=d.charCodeAt(i);ws.send(b)}});

  switchTab(id);
  return tab;
}

/* ========== File Manager Tab ========== */

function fmtSize(b){
  if(b<1024)return b+' B';
  if(b<1024*1024)return (b/1024).toFixed(1)+' KB';
  if(b<1024*1024*1024)return (b/1024/1024).toFixed(1)+' MB';
  return (b/1024/1024/1024).toFixed(1)+' GB';
}

function createFmTab(){
  const id=nextId++;
  const num=nextFmNum++;
  const el=document.createElement('div');
  el.className='fm-container';
  el.innerHTML=`
    <div class="fm-toolbar">
      <button class="fm-btn fm-up-btn" title="Parent directory">&#8593;</button>
      <input class="fm-path" type="text" value="." spellcheck="false">
      <button class="fm-btn fm-new-dir">New Folder</button>
      <label class="fm-btn" style="margin:0">Upload<input type="file" multiple style="display:none"></label>
    </div>
    <div class="fm-body" style="position:relative;flex:1;overflow-y:auto">
      <div class="fm-drop-overlay">Drop files to upload</div>
      <table class="fm-table"><thead><tr>
        <th data-sort="name" style="width:50%">Name</th>
        <th data-sort="size" style="width:15%">Size</th>
        <th data-sort="mtime" style="width:20%">Modified</th>
        <th style="width:15%">Actions</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="fm-editor">
      <div class="fm-editor-bar">
        <span class="fm-editor-name"></span>
        <button class="fm-btn fm-edit-btn">Edit</button>
        <button class="fm-btn fm-save-btn" style="display:none">Save</button>
        <button class="fm-btn fm-close-editor">Close</button>
      </div>
      <textarea spellcheck="false" readonly></textarea>
      <div class="fm-editor-status"><span class="fm-cursor-pos">Ln 1, Col 1</span></div>
    </div>`;
  terminalsEl.appendChild(el);

  const tabEl=makeTabEl('Files #'+num,'fm');
  const tab={id,type:'filemanager',el,tabEl};
  tabs.push(tab);

  tabEl.addEventListener('click',e=>{if(!e.target.classList.contains('close'))switchTab(id)});
  tabEl.querySelector('.close').addEventListener('click',e=>{e.stopPropagation();closeTab(id)});

  // State
  let curPath='.';
  let editPath=null;
  let editDirty=false;
  let editMode=false;
  let sortKey='name';
  let sortAsc=true;
  let selectedRow=null;

  // DOM refs
  const pathInput=el.querySelector('.fm-path');
  const tbody=el.querySelector('tbody');
  const editorPane=el.querySelector('.fm-editor');
  const editorName=el.querySelector('.fm-editor-name');
  const editorTA=el.querySelector('.fm-editor textarea');
  const dropOverlay=el.querySelector('.fm-drop-overlay');
  const fmBody=el.querySelector('.fm-body');
  const editBtn=el.querySelector('.fm-edit-btn');
  const saveBtn=el.querySelector('.fm-save-btn');
  const cursorPos=el.querySelector('.fm-cursor-pos');

  // --- Listing ---
  async function loadDir(p){
    const prev=curPath;
    curPath=p||'.';
    pathInput.value=curPath;
    try{
      const r=await fetch('/api/files?path='+encodeURIComponent(curPath));
      const d=await r.json();
      if(!r.ok){curPath=prev;pathInput.value=prev;return;}
      curPath=d.path;
      pathInput.value=curPath;
      renderList(d.items);
    }catch(e){curPath=prev;pathInput.value=prev}
  }

  function renderList(items){
    items.sort((a,b)=>{
      if(a.is_dir!==b.is_dir)return a.is_dir?-1:1;
      let va,vb;
      if(sortKey==='size'){va=a.size;vb=b.size}
      else if(sortKey==='mtime'){va=a.mtime;vb=b.mtime}
      else{va=a.name.toLowerCase();vb=b.name.toLowerCase()}
      if(va<vb)return sortAsc?-1:1;
      if(va>vb)return sortAsc?1:-1;
      return 0;
    });
    tbody.innerHTML='';
    items.forEach(it=>{
      const tr=document.createElement('tr');
      const icon=it.is_dir?'&#128193;':(it.is_link?'&#128279;':'&#128196;');
      const rel=curPath+'/'+it.name;
      tr.innerHTML=`
        <td><span class="fm-name"><span class="fm-icon">${icon}</span><span class="fm-fname"></span></span></td>
        <td class="fm-size">${it.is_dir?'—':fmtSize(it.size)}</td>
        <td class="fm-mtime">${it.mtime}</td>
        <td class="fm-actions">
          ${!it.is_dir?'<button class="fm-act dl" title="Download">&#8595;</button>':'<button class="fm-act zip" title="Zip">&#128230;</button>'}
          <button class="fm-act ren" title="Rename">&#9998;</button>
          <button class="fm-act del" title="Delete">&#128465;</button>
        </td>`;
      tr.querySelector('.fm-fname').textContent=it.name;

      // Click name -> navigate or edit
      tr.querySelector('.fm-name').addEventListener('click',()=>{
        if(it.is_dir){loadDir(rel)}
        else{openEditor(rel)}
      });

      // Download
      const dlBtn=tr.querySelector('.dl');
      if(dlBtn)dlBtn.addEventListener('click',e=>{e.stopPropagation();window.open('/api/files/download?path='+encodeURIComponent(rel),'_blank')});

      // Zip folder
      const zipBtn=tr.querySelector('.zip');
      if(zipBtn)zipBtn.addEventListener('click',e=>{e.stopPropagation();window.open('/api/files/zip?path='+encodeURIComponent(rel),'_blank')});

      // Rename
      tr.querySelector('.ren').addEventListener('click',e=>{
        e.stopPropagation();
        const nn=prompt('New name:',it.name);
        if(!nn||nn===it.name)return;
        const np=curPath+'/'+nn;
        fetch('/api/files/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_path:rel,new_path:np})}).then(()=>loadDir(curPath));
      });

      // Delete
      tr.querySelector('.del').addEventListener('click',e=>{
        e.stopPropagation();
        if(!confirm('Delete "'+it.name+'"?'))return;
        fetch('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:rel})}).then(()=>loadDir(curPath));
      });

      // Row select
      tr.addEventListener('click',()=>{
        if(selectedRow)selectedRow.classList.remove('selected');
        tr.classList.add('selected');
        selectedRow=tr;
      });

      tbody.appendChild(tr);
    });
  }

  // --- Sorting ---
  el.querySelectorAll('th[data-sort]').forEach(th=>{
    th.addEventListener('click',()=>{
      const k=th.dataset.sort;
      if(sortKey===k)sortAsc=!sortAsc;else{sortKey=k;sortAsc=true}
      loadDir(curPath);
    });
  });

  // --- Navigate ---
  pathInput.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();loadDir(pathInput.value)}});
  el.querySelector('.fm-up-btn').addEventListener('click',()=>{
    if(!curPath||curPath==='/'||curPath==='.')return;
    const parent=curPath.replace(/\/[^\/]*\/?$/,'') || '/';
    loadDir(parent);
  });

  // --- New Folder ---
  el.querySelector('.fm-new-dir').addEventListener('click',()=>{
    const n=prompt('Folder name:');
    if(!n)return;
    const p=curPath+'/'+n;
    fetch('/api/files/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}).then(()=>loadDir(curPath));
  });

  // --- Upload (button) ---
  const fileInput=el.querySelector('input[type=file]');
  fileInput.addEventListener('change',()=>{uploadFiles(fileInput.files);fileInput.value=''});

  // --- Upload (drag & drop) ---
  let dragCnt=0;
  fmBody.addEventListener('dragenter',e=>{e.preventDefault();dragCnt++;dropOverlay.classList.add('show')});
  fmBody.addEventListener('dragleave',e=>{e.preventDefault();dragCnt--;if(dragCnt<=0){dragCnt=0;dropOverlay.classList.remove('show')}});
  fmBody.addEventListener('dragover',e=>e.preventDefault());
  fmBody.addEventListener('drop',e=>{e.preventDefault();dragCnt=0;dropOverlay.classList.remove('show');if(e.dataTransfer.files.length)uploadFiles(e.dataTransfer.files)});

  async function uploadFiles(files){
    const fd=new FormData();
    for(const f of files)fd.append('file',f,f.name);
    await fetch('/api/files/upload?path='+encodeURIComponent(curPath),{method:'POST',body:fd});
    loadDir(curPath);
  }

  // --- Text Editor ---
  function updateCursorPos(){
    const val=editorTA.value;
    const pos=editorTA.selectionStart;
    const before=val.substring(0,pos);
    const line=before.split('\n').length;
    const col=pos-before.lastIndexOf('\n');
    cursorPos.textContent='Ln '+line+', Col '+col;
  }

  function setEditMode(on){
    editMode=on;
    editorTA.readOnly=!on;
    editBtn.style.display=on?'none':'';
    saveBtn.style.display=on?'':'none';
    if(on)editorTA.focus();
  }

  async function openEditor(p){
    try{
      const r=await fetch('/api/files/read?path='+encodeURIComponent(p));
      const d=await r.json();
      if(!r.ok){alert(d.error);return}
      editPath=p;
      editDirty=false;
      editorName.textContent=p.split('/').pop();
      editorName.classList.remove('dirty');
      editorTA.value=d.content;
      // Show notice for binary/large files, hide Edit button
      if(d.notice){
        cursorPos.textContent=d.notice;
        setEditMode(false);
        editBtn.style.display='none';
      } else {
        setEditMode(false);
        editBtn.style.display='';
      }
      editorPane.classList.add('open');
      if(!d.notice)updateCursorPos();
    }catch(e){alert('Error: '+e)}
  }

  editBtn.addEventListener('click',()=>setEditMode(true));

  editorTA.addEventListener('input',()=>{
    if(!editDirty){editDirty=true;editorName.classList.add('dirty')}
    updateCursorPos();
  });

  editorTA.addEventListener('click',updateCursorPos);
  editorTA.addEventListener('keyup',updateCursorPos);
  editorTA.addEventListener('select',updateCursorPos);

  el.querySelector('.fm-save-btn').addEventListener('click',async()=>{
    if(!editPath)return;
    await fetch('/api/files/write',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:editPath,content:editorTA.value})});
    editDirty=false;
    editorName.classList.remove('dirty');
  });

  el.querySelector('.fm-close-editor').addEventListener('click',()=>{
    if(editDirty&&!confirm('Unsaved changes. Close anyway?'))return;
    editorPane.classList.remove('open');
    editPath=null;
    setEditMode(false);
  });

  // Ctrl+S to save
  editorTA.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();el.querySelector('.fm-save-btn').click()}
  });

  // Load initial directory
  loadDir('.');
  switchTab(id);
  return tab;
}

/* ========== Window Resize ========== */

function onResize(){
  const tab=tabs.find(t=>t.id===activeId);
  if(!tab||tab.type!=='terminal')return;
  tab.fitAddon.fit();
  if(tab.ws&&tab.ws.readyState===WebSocket.OPEN)
    tab.ws.send(JSON.stringify({type:'resize',cols:tab.term.cols,rows:tab.term.rows}));
}
window.addEventListener('resize',onResize);
new ResizeObserver(onResize).observe(terminalsEl);

/* ========== Cheat Panel ========== */

const btnCheat=document.getElementById('btn-cheat');
const cheatPanel=document.getElementById('cheat-panel');
const cheatBody=document.getElementById('cheat-body');
const cheatSearch=document.getElementById('cheat-search');
const cheatTitle=document.getElementById('cheat-title');
let cheatData=null;

function toggleCheat(){
  const open=cheatPanel.classList.toggle('open');
  btnCheat.classList.toggle('active',open);
  requestAnimationFrame(onResize);
}

function renderCheat(filter){
  cheatBody.innerHTML='';
  if(!cheatData||!cheatData.groups)return;
  const q=(filter||'').toLowerCase();
  cheatData.groups.forEach(g=>{
    const cmds=g.commands.filter(c=>!q||c.label.toLowerCase().includes(q)||c.cmd.toLowerCase().includes(q));
    if(!cmds.length)return;
    const grp=document.createElement('div');grp.className='cheat-group-name';grp.textContent=g.name;cheatBody.appendChild(grp);
    cmds.forEach(c=>{
      const item=document.createElement('div');item.className='cheat-item';
      item.innerHTML='<span class="cheat-label"></span><span class="cheat-cmd"></span>';
      item.querySelector('.cheat-label').textContent=c.label;
      item.querySelector('.cheat-cmd').textContent=c.cmd;
      item.title=c.cmd;
      item.addEventListener('click',()=>pasteToActive(c.cmd));
      cheatBody.appendChild(item);
    });
  });
}

function unescapeCmd(str){return str.replace(/\\x([0-9a-fA-F]{2})/g,(_,h)=>String.fromCharCode(parseInt(h,16)))}

function pasteToActive(text){
  const tab=tabs.find(t=>t.id===activeId);
  if(!tab||tab.type!=='terminal'||!tab.ws||tab.ws.readyState!==WebSocket.OPEN)return;
  tab.ws.send(new TextEncoder().encode(unescapeCmd(text)));
  tab.term.focus();
}

btnCheat.addEventListener('click',toggleCheat);
cheatSearch.addEventListener('input',()=>renderCheat(cheatSearch.value));

/* ========== Buttons & Shortcuts ========== */

btnNew.addEventListener('click',()=>createTermTab());
btnFm.addEventListener('click',()=>createFmTab());

document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key==='T'){e.preventDefault();createTermTab()}
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key==='W'){e.preventDefault();if(activeId!==null)closeTab(activeId)}
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key==='E'){e.preventDefault();createFmTab()}
  if(e.ctrlKey&&e.key==='Tab'){
    e.preventDefault();
    if(tabs.length<2)return;
    const idx=tabs.findIndex(t=>t.id===activeId);
    switchTab(tabs[(e.shiftKey?(idx-1+tabs.length):(idx+1))%tabs.length].id);
  }
});

/* ========== Init ========== */

const urlParams=new URLSearchParams(location.search);
const initOpts={};
if(urlParams.has('start'))initOpts.startCmd=urlParams.get('start');
if(urlParams.has('display'))initOpts.displayMsg=urlParams.get('display');
createTermTab(Object.keys(initOpts).length?initOpts:undefined);

const __cheatEmbed=__CHEAT_JSON_SLOT__;
if(__cheatEmbed){cheatData=__cheatEmbed;if(cheatData.title)cheatTitle.textContent=cheatData.title;renderCheat();btnCheat.style.display='flex'}

})();
</script>
</body>
</html>"""


def make_app():
    return tornado.web.Application([
        (r"/", IndexHandler),
        (r"/ws", TerminalWebSocket),
        (r"/api/files", FileListHandler),
        (r"/api/files/read", FileReadHandler),
        (r"/api/files/write", FileWriteHandler),
        (r"/api/files/download", FileDownloadHandler),
        (r"/api/files/zip", FileZipHandler),
        (r"/api/files/upload", FileUploadHandler),
        (r"/api/files/mkdir", FileMkdirHandler),
        (r"/api/files/delete", FileDeleteHandler),
        (r"/api/files/rename", FileRenameHandler),
    ])


def load_cheat_file(filepath):
    """Load a YAML cheat file and return its JSON string."""
    global CHEAT_JSON
    if yaml is None:
        print("Warning: PyYAML not installed. Run: pip install pyyaml")
        return
    if not os.path.isfile(filepath):
        print(f"Warning: cheat file not found: {filepath}")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    CHEAT_JSON = json.dumps(data)
    title = data.get("title", filepath)
    groups = data.get("groups", [])
    n_cmds = sum(len(g.get("commands", [])) for g in groups)
    print(f"Loaded cheat sheet: {title} ({len(groups)} groups, {n_cmds} commands)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CheatTerm -- browser-based terminal")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8890,
                        help="Port number (default: 8890)")
    parser.add_argument("--cheat_file", type=str, default=None,
                        help="Path to a YAML cheat sheet file")
    args = parser.parse_args()

    if args.cheat_file:
        load_cheat_file(args.cheat_file)
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        auto_path = os.path.join(script_dir, "cheat.yaml")
        if os.path.isfile(auto_path):
            load_cheat_file(auto_path)

    app = make_app()
    app.listen(args.port, address=args.host)
    print(f"CheatTerm running at http://{args.host}:{args.port}")
    tornado.ioloop.IOLoop.current().start()
