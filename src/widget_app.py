"""
widget_app.py

The real Jarvis desktop widget: a small icon in the corner that expands
into a chat panel in place when clicked, using a single window that
resizes and repositions itself.

Note on resizing: pywebview's own window.resize()/window.move() weren't
reliably taking effect on this frameless window, so position and size
changes go through Windows' SetWindowPos API directly via ctypes instead
- a lower-level approach that's proven more reliable here.

This version wires up the click-to-expand visuals and layout. Actually
connecting the input box to assistant.ask() is the next step once this
is confirmed working.
"""

import ctypes
from ctypes import wintypes

import clr

clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import Screen

import webview

import assistant

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

MARGIN_RIGHT = 200  # horizontal gap from the right edge - the icon's position
MARGIN_BOTTOM = 100  # vertical gap from the bottom edge - the icon's position

# Where the chat panel opens the very FIRST time (before it's ever been
# dragged) - independent of the icon's position above, so you can tune
# this without affecting where the idle icon sits.
CHAT_MARGIN_RIGHT = 20
CHAT_MARGIN_BOTTOM = 20

IDLE_WIDTH = 70
IDLE_HEIGHT = 70
EXPANDED_WIDTH = 280
EXPANDED_HEIGHT = 380


def get_position(width, height):
    """
    Anchors the BOTTOM-RIGHT corner of the window at a fixed point,
    MARGIN_RIGHT/MARGIN_BOTTOM from the screen edge. This deliberately
    keeps that corner fixed so that expanding grows the window upward
    and to the left from the icon - the panel opens "up from" the icon,
    rather than growing from the center.
    """
    work_area = Screen.PrimaryScreen.WorkingArea
    x = work_area.Right - width - MARGIN_RIGHT
    y = work_area.Bottom - height - MARGIN_BOTTOM
    return x, y


def set_bounds(x, y, width, height):
    """
    Sets position and size together in one direct Windows API call,
    bypassing pywebview's own resize/move methods.
    """
    hwnd = ctypes.windll.user32.FindWindowW(None, "Jarvis")
    ctypes.windll.user32.SetWindowPos(
        hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE
    )


def get_current_rect():
    """
    Asks Windows directly where the window actually is right now,
    rather than assuming it's wherever our formula thinks it should be.
    This is what lets a manually dragged position stick - toggling the
    chat panel will grow/shrink around the window's real current spot
    instead of snapping back to a fixed calculated one.
    """
    hwnd = ctypes.windll.user32.FindWindowW(None, "Jarvis")
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


ORB_SVG = """
<svg class="orb-svg" viewBox="0 0 100 100">
  <defs>
    <linearGradient id="tubeGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#B5D4F4"/>
      <stop offset="100%" stop-color="#185FA5"/>
    </linearGradient>
    <radialGradient id="nucleusGrad" cx="35%" cy="30%" r="70%">
      <stop offset="0%" stop-color="#E6F1FB"/>
      <stop offset="100%" stop-color="#185FA5"/>
    </radialGradient>
  </defs>
  <g transform="rotate(0 50 50)">
    <ellipse cx="50" cy="50" rx="40" ry="13" fill="none" stroke="url(#tubeGrad)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <circle r="3" fill="#E6F1FB"><animateMotion dur="3s" repeatCount="indefinite" path="M90,50 A40,13 0 1,1 10,50 A40,13 0 1,1 90,50"/></circle>
  </g>
  <g transform="rotate(60 50 50)">
    <ellipse cx="50" cy="50" rx="40" ry="13" fill="none" stroke="url(#tubeGrad)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <circle r="3" fill="#E6F1FB"><animateMotion dur="3.6s" repeatCount="indefinite" path="M90,50 A40,13 0 1,1 10,50 A40,13 0 1,1 90,50"/></circle>
  </g>
  <g transform="rotate(120 50 50)">
    <ellipse cx="50" cy="50" rx="40" ry="13" fill="none" stroke="url(#tubeGrad)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <circle r="3" fill="#E6F1FB"><animateMotion dur="4.2s" repeatCount="indefinite" path="M90,50 A40,13 0 1,1 10,50 A40,13 0 1,1 90,50"/></circle>
  </g>
  <circle cx="50" cy="50" r="6" fill="url(#nucleusGrad)"/>
</svg>
"""

widget_html = f"""
<html>
<head>
<style>
  html, body {{
    margin: 0;
    padding: 0;
    background: #14141f;
    overflow: hidden;
    font-family: sans-serif;
  }}
  #badge {{
    width: 100%; height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
  }}
  #badge .orb-svg {{ width: 52px; height: 52px; }}
  #panel {{
    display: none;
    flex-direction: column;
    height: 100vh;
    box-sizing: border-box;
    padding: 12px;
    color: #E6F1FB;
  }}
  #panel-header {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding-bottom: 8px;
    border-bottom: 1px solid #2a2a3a;
    cursor: pointer;
  }}
  #panel-header .orb-svg {{ width: 22px; height: 22px; }}
  #messages {{
    flex: 1;
    overflow-y: auto;
    padding: 8px 0;
    font-size: 13px;
  }}
  .msg {{ margin-bottom: 8px; max-width: 85%; padding: 6px 10px; border-radius: 8px; }}
  .msg.you {{ background: #1e1e2e; }}
  .msg.jarvis {{ background: #185FA5; margin-left: auto; }}
  #msg-input {{
    width: 100%;
    box-sizing: border-box;
    background: #1e1e2e;
    border: 1px solid #2a2a3a;
    color: #E6F1FB;
    border-radius: 8px;
    padding: 8px;
    font-size: 13px;
  }}
  @keyframes atomGlow {{
    0%, 100% {{ filter: drop-shadow(0 0 6px rgba(55,138,221,0.6)); }}
    50% {{ filter: drop-shadow(0 0 12px rgba(55,138,221,0.9)); }}
  }}
  .orb-svg {{ animation: atomGlow 2.6s ease-in-out infinite; }}
</style>
</head>
<body>
<div id="badge">
  {ORB_SVG}
</div>
<div id="panel">
  <div id="panel-header" class="pywebview-drag-region">
    {ORB_SVG}
    <span>jarvis</span>
  </div>
  <div id="messages"></div>
  <input id="msg-input" placeholder="">
</div>
<script>
function setChatOpen(open) {{
  document.getElementById('badge').style.display = open ? 'none' : 'flex';
  document.getElementById('panel').style.display = open ? 'flex' : 'none';
}}

function addMessage(sender, text) {{
  const messages = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg ' + sender;
  div.textContent = text;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}}

const msgInput = document.getElementById('msg-input');
msgInput.addEventListener('keydown', function(e) {{
  if (e.key === 'Enter' && msgInput.value.trim() !== '') {{
    const text = msgInput.value.trim();
    msgInput.value = '';
    msgInput.disabled = true;
    addMessage('you', text);
    window.pywebview.api.send_message(text).then(function(response) {{
      addMessage('jarvis', response);
      msgInput.disabled = false;
      msgInput.focus();
    }});
  }}
}});

// Distinguishes a real click from a drag that just ended - dragging
// through pywebview's drag-region still fires a trailing click event
// on release, which would otherwise immediately undo whatever the drag
// just did. Only treat it as a click if the mouse barely moved between
// press and release.
let dragStartX = null;
let dragStartY = null;
let dragMoved = false;

function trackDragStart(e) {{
  dragStartX = e.clientX;
  dragStartY = e.clientY;
  dragMoved = false;
}}

document.addEventListener('mousemove', function(e) {{
  if (dragStartX === null) return;
  if (Math.abs(e.clientX - dragStartX) > 4 || Math.abs(e.clientY - dragStartY) > 4) {{
    dragMoved = true;
  }}
}});

document.addEventListener('mouseup', function(e) {{
  if (dragStartX !== null) {{
    if (dragMoved) {{
      // The chat panel just got dragged somewhere - remember where,
      // so reopening it later goes back to this spot.
      window.pywebview.api.save_position();
    }} else {{
      window.pywebview.api.toggle_chat();
    }}
  }}
  dragStartX = null;
}});

document.getElementById('badge').addEventListener('mousedown', trackDragStart);
document.getElementById('panel-header').addEventListener('mousedown', trackDragStart);
</script>
</body>
</html>
"""

chat_open = False
last_expanded_pos = None  # remembers where the chat panel was last dragged to


class Api:
    def toggle_chat(self):
        global chat_open
        chat_open = not chat_open

        if chat_open:
            width, height = EXPANDED_WIDTH, EXPANDED_HEIGHT
            if last_expanded_pos is not None:
                # Reopen exactly where it was last dragged to.
                x, y = last_expanded_pos
            else:
                # First time opening - its own independent corner spot,
                # controlled by CHAT_MARGIN_RIGHT/CHAT_MARGIN_BOTTOM.
                work_area = Screen.PrimaryScreen.WorkingArea
                x = work_area.Right - width - CHAT_MARGIN_RIGHT
                y = work_area.Bottom - height - CHAT_MARGIN_BOTTOM
        else:
            # Closing ALWAYS returns to the fixed corner - the icon
            # never drifts, regardless of where the panel was dragged.
            width, height = IDLE_WIDTH, IDLE_HEIGHT
            x, y = get_position(IDLE_WIDTH, IDLE_HEIGHT)

        set_bounds(x, y, width, height)
        window.evaluate_js(f"setChatOpen({str(chat_open).lower()})")

    def save_position(self):
        # Only meaningful while the chat panel is open, since the icon
        # itself can no longer be dragged at all.
        global last_expanded_pos
        if chat_open:
            rect = get_current_rect()
            last_expanded_pos = (rect.left, rect.top)

    def send_message(self, message):
        # Same assistant.ask() the CLI (main.py) uses - same memory
        # database too, since both point at the same memory.py file.
        # This is the only new line of actual "brain" logic here; the
        # rest is just wiring it into the widget's chat panel.
        return assistant.ask(message)


window = webview.create_window(
    "Jarvis",
    html=widget_html,
    width=IDLE_WIDTH,
    height=IDLE_HEIGHT,
    frameless=True,
    easy_drag=False,
    on_top=True,
    resizable=False,
    js_api=Api(),
)


def on_shown():
    # Only this very first placement uses the fixed formula - every
    # toggle after this uses the window's real current position instead.
    x, y = get_position(IDLE_WIDTH, IDLE_HEIGHT)
    set_bounds(x, y, IDLE_WIDTH, IDLE_HEIGHT)


window.events.shown += on_shown

webview.start()
