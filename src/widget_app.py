"""
widget_app.py

The real Jarvis desktop widget.

Note on design: pywebview has a confirmed, currently-unresolved bug on
Windows where true window transparency doesn't work reliably. Rather
than keep fighting that, this uses a small solid rounded "badge"
background instead - the same approach real widgets like the Windows
taskbar icons use, not something exotic.

This first version is just the idle icon - the animated atom - with no
chat panel yet. That comes next once this part is confirmed working.
"""

import clr

clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import Screen

import webview

MARGIN_RIGHT = 200  # horizontal gap from the right edge - bigger = further left
MARGIN_BOTTOM = 100  # vertical gap from the bottom edge - bigger = further up
WIDTH = 30
HEIGHT = 30


def get_position():
    """
    MARGIN_RIGHT/MARGIN_BOTTOM define a fixed point on screen - the
    CENTER the widget sits on - independent of WIDTH/HEIGHT. Resizing
    the widget grows or shrinks it evenly around that same point,
    instead of shifting it, since the top-left corner is calculated
    from the center outward using whatever the current size is.
    """
    work_area = Screen.PrimaryScreen.WorkingArea
    center_x = work_area.Right - MARGIN_RIGHT
    center_y = work_area.Bottom - MARGIN_BOTTOM
    x = center_x - WIDTH // 2
    y = center_y - HEIGHT // 2
    return x, y


widget_html = """
<html>
<head>
<style>
  html, body {
    margin: 0;
    padding: 0;
    background: #14141f;
    overflow: hidden;
  }
  #badge {
    width: 100%; height: 100%;
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #14141f;
  }
  @keyframes atomGlow {
    0%, 100% { filter: drop-shadow(0 0 6px rgba(55,138,221,0.6)); }
    50% { filter: drop-shadow(0 0 12px rgba(55,138,221,0.9)); }
  }
  #orb { width: 52px; height: 52px; animation: atomGlow 2.6s ease-in-out infinite; }
</style>
</head>
<body>
<div id="badge">
  <svg id="orb" viewBox="0 0 100 100">
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
      <ellipse cx="50" cy="50" rx="40" ry="13" fill="none" stroke="url(#tubeGrad)" stroke-width="3.5"/>
      <circle r="3" fill="#E6F1FB"><animateMotion dur="3s" repeatCount="indefinite" path="M90,50 A40,13 0 1,1 10,50 A40,13 0 1,1 90,50"/></circle>
    </g>
    <g transform="rotate(60 50 50)">
      <ellipse cx="50" cy="50" rx="40" ry="13" fill="none" stroke="url(#tubeGrad)" stroke-width="3.5"/>
      <circle r="3" fill="#E6F1FB"><animateMotion dur="3.6s" repeatCount="indefinite" path="M90,50 A40,13 0 1,1 10,50 A40,13 0 1,1 90,50"/></circle>
    </g>
    <g transform="rotate(120 50 50)">
      <ellipse cx="50" cy="50" rx="40" ry="13" fill="none" stroke="url(#tubeGrad)" stroke-width="3.5"/>
      <circle r="3" fill="#E6F1FB"><animateMotion dur="4.2s" repeatCount="indefinite" path="M90,50 A40,13 0 1,1 10,50 A40,13 0 1,1 90,50"/></circle>
    </g>
    <circle cx="50" cy="50" r="6" fill="url(#nucleusGrad)"/>
  </svg>
</div>
</body>
</html>
"""

window = webview.create_window(
    "Jarvis",
    html=widget_html,
    width=WIDTH,
    height=HEIGHT,
    frameless=True,
    easy_drag=False,
    on_top=True,
    resizable=False,
)


def position_and_size_window():
    # Resize first, then position - resizing can shift where the
    # window's top-left corner ends up, so positioning has to happen
    # after, using the final size, not before it.
    window.resize(WIDTH, HEIGHT)
    x, y = get_position()
    window.move(x, y)


window.events.shown += position_and_size_window

webview.start()
