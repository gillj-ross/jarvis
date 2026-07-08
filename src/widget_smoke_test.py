"""
widget_smoke_test.py

Bare minimum check: does pywebview actually open a window on this
machine? Nothing fancy yet - just confirms the library and its Windows
backend work before we build the real widget on top of it. Delete this
file once the real widget is up and running.
"""

import webview

webview.create_window("Jarvis smoke test", html="<h1>It works</h1>")
webview.start()
