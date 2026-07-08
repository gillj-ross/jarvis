"""
position_debug.py

One-off diagnostic - prints the actual numbers Windows and pywebview
are reporting for your screen, so we can see real data instead of
guessing why the widget keeps ending up in the wrong place. Delete
this file once the positioning bug is actually fixed.
"""

import clr

clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import Screen

import webview

screen = Screen.PrimaryScreen
print("=== .NET Screen.PrimaryScreen ===")
print(f"Bounds:      {screen.Bounds}")
print(f"WorkingArea: {screen.WorkingArea}")

print()
print("=== pywebview.screens ===")
for i, s in enumerate(webview.screens):
    print(f"Screen {i}: width={s.width}, height={s.height}")

WIDTH = 64
HEIGHT = 64
MARGIN = 50

work_area = screen.WorkingArea
x = work_area.Right - WIDTH - MARGIN
y = work_area.Bottom - HEIGHT - MARGIN

print()
print("=== Computed position ===")
print(f"x = {x}, y = {y}")
print(f"Widget would span from ({x}, {y}) to ({x + WIDTH}, {y + HEIGHT})")
