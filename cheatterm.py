"""CheatTerm -- Browser-based terminal with WebSocket + PTY.

Architecture:
  - Tornado WebSocket server
  - PTY (pseudo-terminal) per connection via pty.fork()
  - xterm.js frontend (loaded from CDN) with multi-tab support
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
import tornado.ioloop
import tornado.web
import tornado.websocket

try:
    import yaml
except ImportError:
    yaml = None

SHELL = os.environ.get("SHELL", "/bin/bash")
CHEAT_JSON = "null"  # Pre-loaded cheat data as JSON string


class TerminalWebSocket(tornado.websocket.WebSocketHandler):
    """One WebSocket = one PTY + child bash process."""

    def open(self):
        """Fork a child process with a PTY."""
        self.child_pid, self.fd = pty.fork()

        if self.child_pid == 0:
            os.execvpe(SHELL, [SHELL], os.environ)
        else:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._set_winsize(80, 24)
            self.io_loop = tornado.ioloop.IOLoop.current()
            self.io_loop.add_handler(
                self.fd, self._on_pty_read, tornado.ioloop.IOLoop.READ
            )

    def _set_winsize(self, cols, rows):
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
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
  --base:    #1e1e2e;
  --mantle:  #181825;
  --crust:   #11111b;
  --surface0:#313244;
  --surface1:#45475a;
  --text:    #cdd6f4;
  --subtext: #a6adc8;
  --blue:    #89b4fa;
  --green:   #a6e3a1;
  --pink:    #f5c2e7;
  --red:     #f38ba8;
  --yellow:  #f9e2af;
  --overlay: #6c7086;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:var(--base);font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden}

/* ---- Tab Bar ---- */
#tab-bar{
  display:flex;
  align-items:center;
  background:var(--crust);
  height:38px;
  padding:0 4px;
  gap:2px;
  border-bottom:1px solid var(--surface0);
  user-select:none;
  -webkit-user-select:none;
}
#tab-list{
  display:flex;
  align-items:center;
  gap:2px;
  flex:1;
  overflow-x:auto;
  scrollbar-width:none;
}
#tab-list::-webkit-scrollbar{display:none}

.tab{
  display:flex;
  align-items:center;
  gap:6px;
  padding:0 12px;
  height:30px;
  background:var(--mantle);
  color:var(--subtext);
  border-radius:6px 6px 0 0;
  font-size:12px;
  font-weight:500;
  cursor:pointer;
  white-space:nowrap;
  flex-shrink:0;
  transition:background .15s,color .15s;
  border:1px solid transparent;
  border-bottom:none;
}
.tab:hover{
  background:var(--surface0);
  color:var(--text);
}
.tab.active{
  background:var(--base);
  color:var(--text);
  border-color:var(--surface0);
}
.tab .dot{
  width:8px;height:8px;
  border-radius:50%;
  background:var(--green);
  flex-shrink:0;
}
.tab.dead .dot{background:var(--red)}
.tab .close{
  display:flex;
  align-items:center;
  justify-content:center;
  width:18px;height:18px;
  border-radius:4px;
  font-size:14px;
  line-height:1;
  color:var(--overlay);
  opacity:0;
  transition:opacity .15s,background .15s,color .15s;
}
.tab:hover .close{opacity:1}
.tab .close:hover{
  background:var(--red);
  color:var(--crust);
}

#btn-new{
  display:flex;
  align-items:center;
  justify-content:center;
  width:30px;height:30px;
  background:none;
  border:none;
  color:var(--overlay);
  font-size:18px;
  cursor:pointer;
  border-radius:6px;
  flex-shrink:0;
  transition:background .15s,color .15s;
}
#btn-new:hover{
  background:var(--surface0);
  color:var(--text);
}

/* ---- Terminal Containers ---- */
#main{
  display:flex;
  height:calc(100% - 38px);
  width:100%;
}
#terminals{
  position:relative;
  flex:1;
  min-width:0;
}
.term-container{
  position:absolute;
  inset:0;
  display:none;
}
.term-container.active{
  display:block;
}

/* ---- Cheat Panel ---- */
#btn-cheat{
  display:none;
  align-items:center;
  justify-content:center;
  width:30px;height:30px;
  background:none;
  border:none;
  color:var(--overlay);
  font-size:15px;
  cursor:pointer;
  border-radius:6px;
  flex-shrink:0;
  transition:background .15s,color .15s;
  margin-right:2px;
}
#btn-cheat:hover{
  background:var(--surface0);
  color:var(--text);
}
#btn-cheat.active{
  color:var(--blue);
}

#cheat-panel{
  width:280px;
  flex-shrink:0;
  background:var(--mantle);
  border-left:1px solid var(--surface0);
  display:none;
  flex-direction:column;
  overflow:hidden;
}
#cheat-panel.open{
  display:flex;
}
#cheat-header{
  padding:10px 12px 8px;
  font-size:13px;
  font-weight:600;
  color:var(--text);
  border-bottom:1px solid var(--surface0);
  display:flex;
  align-items:center;
  gap:6px;
}
#cheat-header .cheat-icon{
  font-size:15px;
}
#cheat-search{
  margin:8px;
  padding:6px 10px;
  background:var(--surface0);
  border:1px solid var(--surface1);
  border-radius:6px;
  color:var(--text);
  font-size:12px;
  font-family:inherit;
  outline:none;
}
#cheat-search::placeholder{color:var(--overlay)}
#cheat-search:focus{border-color:var(--blue)}
#cheat-body{
  flex:1;
  overflow-y:auto;
  padding:4px 0;
  scrollbar-width:thin;
  scrollbar-color:var(--surface1) transparent;
}
.cheat-group-name{
  padding:8px 12px 4px;
  font-size:11px;
  font-weight:600;
  color:var(--overlay);
  text-transform:uppercase;
  letter-spacing:.5px;
}
.cheat-item{
  display:flex;
  flex-direction:column;
  padding:6px 12px;
  cursor:pointer;
  transition:background .12s;
}
.cheat-item:hover{
  background:var(--surface0);
}
.cheat-item .cheat-label{
  font-size:12px;
  color:var(--text);
  font-weight:500;
}
.cheat-item .cheat-cmd{
  font-size:11px;
  color:var(--overlay);
  font-family:'JetBrains Mono','Fira Code','Courier New',monospace;
  margin-top:1px;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
}
</style>
</head>
<body>

<!-- Tab Bar -->
<div id="tab-bar">
  <div id="tab-list"></div>
  <button id="btn-new" title="New terminal (Ctrl+Shift+T)">+</button>
  <button id="btn-cheat" title="Command cheat sheet">&#9776;</button>
</div>

<!-- Main area: terminals + cheat panel -->
<div id="main">
  <div id="terminals"></div>
  <div id="cheat-panel">
    <div id="cheat-header"><span class="cheat-icon">&#9776;</span><span id="cheat-title">Commands</span></div>
    <input id="cheat-search" type="text" placeholder="Search commands...">
    <div id="cheat-body"></div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/lib/addon-web-links.min.js"></script>
<script>
(function(){

const THEME = {
  background:  '#1e1e2e',
  foreground:  '#cdd6f4',
  cursor:      '#f5c2e7',
  cursorAccent:'#1e1e2e',
  black:       '#45475a',
  red:         '#f38ba8',
  green:       '#a6e3a1',
  yellow:      '#f9e2af',
  blue:        '#89b4fa',
  magenta:     '#f5c2e7',
  cyan:        '#94e2d5',
  white:       '#bac2de',
  brightBlack: '#585b70',
  brightRed:   '#f38ba8',
  brightGreen: '#a6e3a1',
  brightYellow:'#f9e2af',
  brightBlue:  '#89b4fa',
  brightMagenta:'#f5c2e7',
  brightCyan:  '#94e2d5',
  brightWhite: '#a6adc8',
  selectionBackground:'rgba(137,180,250,0.3)',
};

const tabListEl   = document.getElementById('tab-list');
const terminalsEl = document.getElementById('terminals');
const btnNew      = document.getElementById('btn-new');

let tabs     = [];   // {id, term, fitAddon, ws, el, tabEl, alive}
let activeId = null;
let nextId   = 1;

/* ---- Create Tab ---- */
function createTab(opts){
  opts = opts || {};
  const id = nextId++;

  // Terminal container
  const el = document.createElement('div');
  el.className = 'term-container';
  el.id = 'tc-'+id;
  terminalsEl.appendChild(el);

  // xterm.js
  const term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "'JetBrains Mono','Fira Code','Courier New',monospace",
    theme: THEME,
    allowProposedApi: true,
  });
  const fitAddon = new FitAddon.FitAddon();
  const webLinksAddon = new WebLinksAddon.WebLinksAddon(
    (ev, uri) => { window.open(uri, '_blank', 'noopener'); }
  );
  term.loadAddon(fitAddon);
  term.loadAddon(webLinksAddon);
  term.open(el);

  // Tab element
  const tabEl = document.createElement('div');
  tabEl.className = 'tab';
  tabEl.innerHTML =
    '<span class="dot"></span>' +
    '<span class="label">Terminal #'+id+'</span>' +
    '<span class="close">&times;</span>';
  tabListEl.appendChild(tabEl);

  // Tab object
  const tab = { id, term, fitAddon, ws:null, el, tabEl, alive:true };
  tabs.push(tab);

  // Click tab -> switch
  tabEl.addEventListener('click', (e) => {
    if(e.target.classList.contains('close')) return;
    switchTab(id);
  });

  // Close button
  tabEl.querySelector('.close').addEventListener('click', (e) => {
    e.stopPropagation();
    closeTab(id);
  });

  // WebSocket
  const proto = location.protocol==='https:'?'wss:':'ws:';
  const ws = new WebSocket(proto+'//'+location.host+'/ws');
  ws.binaryType = 'arraybuffer';
  tab.ws = ws;

  ws.onopen = () => {
    fitAddon.fit();
    ws.send(JSON.stringify({type:'resize',cols:term.cols,rows:term.rows}));
    if(activeId===id) term.focus();

    // Display message (local-only, written to terminal before shell output)
    if(opts.displayMsg){
      term.write('\x1b[36m' + opts.displayMsg + '\x1b[0m\r\n');
    }
    // Start command (sent to PTY after shell has time to initialize)
    if(opts.startCmd){
      setTimeout(() => {
        if(ws.readyState===WebSocket.OPEN){
          ws.send(new TextEncoder().encode(opts.startCmd + '\n'));
        }
      }, 300);
    }
  };

  ws.onmessage = (ev) => {
    if(ev.data instanceof ArrayBuffer){
      term.write(new Uint8Array(ev.data));
    } else {
      term.write(ev.data);
    }
  };

  ws.onclose = () => {
    tab.alive = false;
    tabEl.classList.add('dead');
    term.write('\r\n\x1b[31m[Process exited]\x1b[0m\r\n');
  };

  // Keystrokes -> WS
  term.onData((data) => {
    if(ws.readyState===WebSocket.OPEN){
      ws.send(new TextEncoder().encode(data));
    }
  });
  term.onBinary((data) => {
    if(ws.readyState===WebSocket.OPEN){
      const buf = new Uint8Array(data.length);
      for(let i=0;i<data.length;i++) buf[i]=data.charCodeAt(i);
      ws.send(buf);
    }
  });

  // Switch to the new tab
  switchTab(id);
  return tab;
}

/* ---- Switch Tab ---- */
function switchTab(id){
  activeId = id;
  tabs.forEach(t => {
    const isActive = t.id === id;
    t.el.classList.toggle('active', isActive);
    t.tabEl.classList.toggle('active', isActive);
    if(isActive){
      requestAnimationFrame(() => {
        t.fitAddon.fit();
        t.term.focus();
        if(t.ws && t.ws.readyState===WebSocket.OPEN){
          t.ws.send(JSON.stringify({type:'resize',cols:t.term.cols,rows:t.term.rows}));
        }
      });
    }
  });
}

/* ---- Close Tab ---- */
function closeTab(id){
  const idx = tabs.findIndex(t=>t.id===id);
  if(idx===-1) return;
  const tab = tabs[idx];

  // Kill connection -> server kills PTY
  if(tab.ws && tab.ws.readyState===WebSocket.OPEN){
    tab.ws.close();
  }
  tab.term.dispose();
  tab.el.remove();
  tab.tabEl.remove();
  tabs.splice(idx, 1);

  // Switch to neighbor or create new if empty
  if(tabs.length===0){
    createTab();
  } else if(activeId===id){
    const next = tabs[Math.min(idx, tabs.length-1)];
    switchTab(next.id);
  }
}

/* ---- Window Resize ---- */
function onResize(){
  const tab = tabs.find(t=>t.id===activeId);
  if(!tab) return;
  tab.fitAddon.fit();
  if(tab.ws && tab.ws.readyState===WebSocket.OPEN){
    tab.ws.send(JSON.stringify({type:'resize',cols:tab.term.cols,rows:tab.term.rows}));
  }
}
window.addEventListener('resize', onResize);
new ResizeObserver(onResize).observe(terminalsEl);

/* ---- Cheat Panel ---- */
const btnCheat    = document.getElementById('btn-cheat');
const cheatPanel  = document.getElementById('cheat-panel');
const cheatBody   = document.getElementById('cheat-body');
const cheatSearch = document.getElementById('cheat-search');
const cheatTitle  = document.getElementById('cheat-title');
let cheatData     = null;  // {title, groups:[{name, commands:[{label,cmd}]}]}

function toggleCheat(){
  const open = cheatPanel.classList.toggle('open');
  btnCheat.classList.toggle('active', open);
  // Re-fit active terminal after panel toggle
  requestAnimationFrame(onResize);
}

function renderCheat(filter){
  cheatBody.innerHTML = '';
  if(!cheatData || !cheatData.groups) return;
  const q = (filter||'').toLowerCase();
  cheatData.groups.forEach(g => {
    const cmds = g.commands.filter(c =>
      !q || c.label.toLowerCase().includes(q) || c.cmd.toLowerCase().includes(q)
    );
    if(cmds.length === 0) return;
    const grp = document.createElement('div');
    grp.className = 'cheat-group-name';
    grp.textContent = g.name;
    cheatBody.appendChild(grp);
    cmds.forEach(c => {
      const item = document.createElement('div');
      item.className = 'cheat-item';
      item.innerHTML =
        '<span class="cheat-label"></span>' +
        '<span class="cheat-cmd"></span>';
      item.querySelector('.cheat-label').textContent = c.label;
      item.querySelector('.cheat-cmd').textContent = c.cmd;
      item.title = c.cmd;
      item.addEventListener('click', () => pasteToActive(c.cmd));
      cheatBody.appendChild(item);
    });
  });
}

function unescapeCmd(str){
  // Convert \xNN escape sequences to actual characters
  return str.replace(/\\x([0-9a-fA-F]{2})/g,
    (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

function pasteToActive(text, autoExec){
  const tab = tabs.find(t=>t.id===activeId);
  if(!tab || !tab.ws || tab.ws.readyState!==WebSocket.OPEN) return;
  const resolved = unescapeCmd(text);
  tab.ws.send(new TextEncoder().encode(resolved));
  tab.term.focus();
}

btnCheat.addEventListener('click', toggleCheat);
cheatSearch.addEventListener('input', () => renderCheat(cheatSearch.value));

/* ---- Keyboard Shortcuts ---- */
btnNew.addEventListener('click', () => createTab());

document.addEventListener('keydown', (e) => {
  // Ctrl+Shift+T -> new tab
  if((e.ctrlKey||e.metaKey) && e.shiftKey && e.key==='T'){
    e.preventDefault();
    createTab();
  }
  // Ctrl+Shift+W -> close tab
  if((e.ctrlKey||e.metaKey) && e.shiftKey && e.key==='W'){
    e.preventDefault();
    if(activeId!==null) closeTab(activeId);
  }
  // Ctrl+Tab / Ctrl+Shift+Tab -> cycle tabs
  if(e.ctrlKey && e.key==='Tab'){
    e.preventDefault();
    if(tabs.length<2) return;
    const idx = tabs.findIndex(t=>t.id===activeId);
    const next = e.shiftKey
      ? (idx-1+tabs.length)%tabs.length
      : (idx+1)%tabs.length;
    switchTab(tabs[next].id);
  }
});

/* ---- Start with one tab (parse URL params for first tab) ---- */
const urlParams = new URLSearchParams(location.search);
const initOpts = {};
if(urlParams.has('start'))   initOpts.startCmd   = urlParams.get('start');
if(urlParams.has('display')) initOpts.displayMsg  = urlParams.get('display');
createTab(Object.keys(initOpts).length ? initOpts : undefined);

// Load cheat sheet embedded by server (--cheat_file)
const __cheatEmbed = __CHEAT_JSON_SLOT__;
if(__cheatEmbed){
  cheatData = __cheatEmbed;
  if(cheatData.title) cheatTitle.textContent = cheatData.title;
  renderCheat();
  btnCheat.style.display = 'flex';
}

})();
</script>
</body>
</html>"""


def make_app():
    return tornado.web.Application([
        (r"/", IndexHandler),
        (r"/ws", TerminalWebSocket),
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
        # Auto-load cheat.yaml from same directory as this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        auto_path = os.path.join(script_dir, "cheat.yaml")
        if os.path.isfile(auto_path):
            load_cheat_file(auto_path)

    app = make_app()
    app.listen(args.port, address=args.host)
    print(f"CheatTerm running at http://{args.host}:{args.port}")
    tornado.ioloop.IOLoop.current().start()
