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
import io
import json
import threading
import traceback
import wave
from ctypes import wintypes

import clr

clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import Screen

import numpy as np
import sounddevice as sd
import webview
from openwakeword.model import Model as WakeWordModel
from openwakeword.utils import download_models as download_wake_word_models

import assistant
import tts

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

# Mono, 16kHz is plenty for speech and keeps recordings small - matches
# what ElevenLabs' Scribe model expects to transcribe well.
MIC_SAMPLE_RATE = 16000

# Holds audio chunks while a recording is in progress, and the active
# input stream so stop_recording() can shut it down cleanly.
recording_frames = []
recording_stream = None


def _on_audio_block(indata, frames, time_info, status):
    # Called automatically by sounddevice on a background thread every
    # time it has a new chunk of mic audio - just stash a copy of it.
    recording_frames.append(indata.copy())


# --- Wake word ("Jarvis") -----------------------------------------------
#
# openWakeWord runs entirely offline, no account or API key needed. Its
# only pre-trained model for this is "hey jarvis" - there's no official
# model trained on plain "Jarvis" alone. Per openWakeWord's own docs,
# this model will still often catch "Jarvis" by itself, just with a
# somewhat higher miss rate than the full "Hey Jarvis" phrase - the
# lower threshold below leans into that tolerance a bit. If misses are
# still too frequent in practice, the real fix is training a dedicated
# "Jarvis"-only model (openWakeWord supports this, but it's a separate,
# heavier undertaking involving generating synthetic training clips).
WAKE_WORD_NAME = "hey_jarvis"
WAKE_WORD_THRESHOLD = 0.35
WAKE_CHUNK = 1280  # 80ms at 16kHz - openWakeWord's native frame size

# Said mid-conversation, any of these tell Jarvis to stop listening and
# wait for the wake word again, silently - no spoken reply, matching
# how he already stays quiet when the follow_up window times out.
# Substring match against the transcript, so phrasing around them
# (e.g. "Jarvis, give me a second to think") still matches.
STAND_DOWN_PHRASES = [
    "give me a second",
    "give me a sec",
    "give me a minute",
    "give me a moment",
    "hold on",
    "one moment",
    "one second",
    "stop listening",
    "that will be all",
    "that'll be all",
    "never mind for now",
    "shut up",
    "shut it",
    "be quiet",
    "quiet please",
    "stop talking",
    "hush",
    "zip it",
    "enough",
    "that's enough",
    "mute",
]


def _is_stand_down(transcript: str) -> bool:
    normalized = transcript.lower()
    return any(phrase in normalized for phrase in STAND_DOWN_PHRASES)


# Unlike STAND_DOWN_PHRASES, these still get a normal spoken reply -
# they're natural sign-offs, not a request to pause - but checked
# against BOTH what you said and what Jarvis says back, since either
# side can end a conversation ("talk to you later" from you, or
# "enjoy your evening" from him). Either match skips the follow_up
# window entirely and goes straight back to idle once he's done
# speaking, instead of listening for a few more seconds afterward.
CONVERSATION_END_PHRASES = [
    "talk to you later",
    "talk later",
    "see you later",
    "see you soon",
    "see ya",
    "catch you later",
    "catch ya later",
    "goodbye",
    "bye for now",
    "bye now",
    "goodnight",
    "good night",
    "sleep well",
    "rest well",
    "farewell",
    "cheers",
    "peace out",
    "that's all for now",
    "that is all for now",
    "that's it for now",
    "that will do",
    "that'll do",
    "nothing else",
    "nothing more",
    "that covers it",
    "that covers everything",
    "i'm done",
    "i am done",
    "i'm all set",
    "i am all set",
    "all set",
    "we're done",
    "we are done",
    "we're good",
    "we are good",
    "i'm good",
    "i am good",
    "i'm finished",
    "i am finished",
    "gotta go",
    "got to go",
    "have a good night",
    "have a good evening",
    "have a great day",
    "have a great evening",
    "have a good one",
    "enjoy your evening",
    "enjoy your day",
    "enjoy your night",
    "take care",
    "talk soon",
    "until next time",
    "thanks, that's all",
    "thank you, that's all",
    "thanks for your help",
    "appreciate it",
    "appreciate the help",
    "thanks, buddy",
]


def _is_conversation_end(text: str) -> bool:
    normalized = text.lower()
    return any(phrase in normalized for phrase in CONVERSATION_END_PHRASES)


# Silence-based cutoff for the command that follows the wake word -
# rather than always recording a fixed length, stop as soon as enough
# quiet is detected after you've started talking (same idea as Siri/
# Alexa), with a hard cap as a safety net in case silence never reads
# as quiet enough (e.g. in a noisy room).
SILENCE_SECONDS = 1.8  # how much continuous quiet means "done talking"
MIN_COMMAND_SECONDS = 0.6  # ignore silence checks until at least this much is recorded
MAX_COMMAND_SECONDS = 15  # hard cap regardless of silence detection

# After Jarvis finishes replying, he listens (without needing the wake
# word again) for this long - if you keep talking within this window,
# it counts as a follow-up and the conversation continues; if you stay
# quiet for the whole window, he goes back to requiring "Jarvis" again.
FOLLOW_UP_SECONDS = 2.2

SILENCE_CHUNKS_NEEDED = int(SILENCE_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)
MIN_COMMAND_CHUNKS = int(MIN_COMMAND_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)
MAX_COMMAND_CHUNKS = int(MAX_COMMAND_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)
FOLLOW_UP_CHUNKS = int(FOLLOW_UP_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)


def _calibrate_silence_threshold():
    """
    A fixed "quiet enough" volume level is really a guess about your
    specific mic and room - too low and background noise never reads
    as silence (which was very likely why responses were arriving so
    late: it was hitting the 15-second hard cap almost every time
    instead of detecting you'd stopped talking). This records ~1
    second of whatever's normal in the room right now and sets the
    threshold relative to that instead of a fixed guess.
    """
    try:
        sample = sd.rec(
            int(1.0 * MIC_SAMPLE_RATE), samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16"
        )
        sd.wait()
        noise_floor = float(np.sqrt(np.mean(sample.astype(np.float32) ** 2)))
        # Multiplier raised from 2.5x to 3.5x, and the hard floor from 150
        # to 250 - a click or key clack easily clears a threshold that's
        # only just above the room's ambient hum, so pushing the bar
        # higher means it takes something closer to actual speaking
        # volume to register as "loud" at all, before the consecutive-
        # frame debounce below even comes into play.
        threshold = max(noise_floor * 3.5, 250.0)
        print(f"Ambient noise floor: {noise_floor:.0f} -> silence threshold: {threshold:.0f}")
        return threshold
    except Exception as e:
        print(f"Silence calibration failed ({e}), using default threshold of 400")
        return 400.0


print("Loading wake-word model (first run downloads it, may take a moment)...")
download_wake_word_models(model_names=[WAKE_WORD_NAME])
wake_model = WakeWordModel(wakeword_models=[WAKE_WORD_NAME], inference_framework="onnx")

print("Calibrating silence detection to your room...")
SILENCE_RMS_THRESHOLD = _calibrate_silence_threshold()

# The whole listening cycle as one explicit state, rather than a couple
# of overlapping booleans - easier to reason about now that there are
# four distinct phases:
#   idle       - only listening for the wake word itself
#   capturing  - actively recording your command, watching for silence
#   processing - transcribing/thinking/speaking - mic fully ignored
#   follow_up  - reply just finished; listening for you to keep talking
#                without needing to say "Jarvis" again
wake_lock = threading.Lock()
wake_state = "idle"
wake_command_frames = []
wake_silence_run = 0  # consecutive quiet chunks seen since capture started
wake_follow_up_count = 0  # chunks elapsed since entering the follow_up window
wake_loud_run = 0  # consecutive loud chunks seen right now
wake_after_playback = "follow_up"  # what to do once the current reply finishes playing
wake_stream = None

# A mouse click, keyboard clack, or similar brief pop is loud enough to
# cross SILENCE_RMS_THRESHOLD for a single 80ms frame, but it isn't
# sustained the way actual speech is. Requiring this many CONSECUTIVE
# loud frames before counting something as "real speech" filters those
# out - without this, a single click was being treated as "you started
# talking again" during follow_up (restarting listening even though
# you'd gone quiet), and was also resetting the silence timer during
# capturing, so he'd never detect you'd finished talking. Raised from 2
# to 4, then to 6, after clicks/ambient noise were still occasionally
# tripping a false start - 6 frames (~480ms) still catches real speech
# almost immediately, but a click or a burst of typing has to sustain
# for nearly half a second to fake it now, which real keystrokes and
# clicks essentially never do.
LOUD_FRAMES_THRESHOLD = 6


def _rms(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


def _start_capturing(initial_chunk=None):
    # Common entry point for beginning to record a command, whether
    # triggered by the wake word (from idle) or by you simply talking
    # again during the follow_up window (no wake word needed there).
    global wake_state, wake_command_frames, wake_silence_run, wake_loud_run
    with wake_lock:
        wake_state = "capturing"
        wake_command_frames = [initial_chunk] if initial_chunk is not None else []
    wake_silence_run = 0
    wake_loud_run = 0
    _open_chat_panel()
    window.evaluate_js("document.getElementById('mic-btn').classList.add('recording')")


def _enter_idle():
    # Back to square one: only "Jarvis"/"Hey Jarvis" wakes him up again.
    global wake_state, wake_command_frames, wake_silence_run, wake_follow_up_count, wake_loud_run
    with wake_lock:
        wake_state = "idle"
        wake_command_frames = []
    wake_silence_run = 0
    wake_follow_up_count = 0
    wake_loud_run = 0
    window.evaluate_js("document.getElementById('mic-btn').classList.remove('recording')")


def _enter_follow_up():
    # Called once a reply has actually finished playing out loud (see
    # Api.playback_finished()). Gives you a short window to keep
    # talking without repeating the wake word - if you do, _start_
    # capturing() takes over immediately; if not, _wake_word_callback
    # times it out back to _enter_idle().
    global wake_state, wake_follow_up_count, wake_loud_run
    with wake_lock:
        wake_state = "follow_up"
    wake_follow_up_count = 0
    wake_loud_run = 0
    window.evaluate_js("document.getElementById('mic-btn').classList.add('recording')")


def _wake_word_callback(indata, frames, time_info, status):
    # Runs continuously in the background on its own thread, dispatching
    # on the current phase of the conversation:
    #   idle       - score every 80ms chunk against the wake-word model
    #   capturing  - buffer audio until enough trailing silence (or the
    #                hard cap) is seen, then hand off to be transcribed
    #   processing - fully ignored; Jarvis is transcribing/thinking/
    #                speaking and can't hear (or react to) his own voice
    #   follow_up  - listening for you to start talking again without
    #                needing the wake word; times out back to idle
    # IMPORTANT: sounddevice silently ABORTS this entire stream forever
    # if this function ever raises - with no visible error unless
    # logging happens to be configured for it, which by default it
    # isn't. That's what caused Jarvis to go quiet and stuck with the
    # mic button lit previously: one bad frame killed the mic
    # permanently until the app was relaunched. Wrapping everything
    # below means a single hiccup gets logged and skipped instead of
    # ending wake-word detection for the rest of the session.
    global wake_state, wake_command_frames, wake_silence_run, wake_follow_up_count, wake_loud_run

    try:
        with wake_lock:
            state = wake_state

        chunk = indata[:, 0].copy()

        if state == "idle":
            prediction = wake_model.predict(chunk)
            if prediction.get(WAKE_WORD_NAME, 0.0) > WAKE_WORD_THRESHOLD:
                _start_capturing()
            return

        if state == "processing":
            # Mic is fully ignored while Jarvis is transcribing,
            # thinking, or speaking - he can't hear (and react to) his
            # own voice, and can't be interrupted mid-response.
            return

        # Shared "is this actually speech, or just a click/pop" check for
        # follow_up and capturing below - see LOUD_FRAMES_THRESHOLD.
        if _rms(chunk) >= SILENCE_RMS_THRESHOLD:
            wake_loud_run += 1
        else:
            wake_loud_run = 0
        is_real_speech = wake_loud_run >= LOUD_FRAMES_THRESHOLD

        if state == "follow_up":
            wake_follow_up_count += 1
            if is_real_speech:
                # You started talking again - no wake word needed.
                _start_capturing(initial_chunk=chunk)
            elif wake_follow_up_count >= FOLLOW_UP_CHUNKS:
                _enter_idle()
            return

        if state == "capturing":
            wake_command_frames.append(chunk)
            chunk_count = len(wake_command_frames)

            if is_real_speech:
                wake_silence_run = 0
            else:
                wake_silence_run += 1

            hit_silence = chunk_count >= MIN_COMMAND_CHUNKS and wake_silence_run >= SILENCE_CHUNKS_NEEDED
            hit_max = chunk_count >= MAX_COMMAND_CHUNKS

            if hit_silence or hit_max:
                frames_to_process = wake_command_frames
                with wake_lock:
                    wake_state = "processing"
                    wake_command_frames = []
                wake_silence_run = 0
                threading.Thread(
                    target=_process_wake_command, args=(frames_to_process,), daemon=True
                ).start()
    except Exception:
        print("Wake-word callback error (recovered, mic stays live):")
        traceback.print_exc()
        _enter_idle()


def _speak_reply(reply_text, end_check_text=""):
    """
    Synthesizes and plays a reply, deciding whether to open the
    follow_up window or go straight to idle afterward based on whether
    this sounds like the end of the conversation. Returns True if
    handed off to playback, False if speech synthesis failed.
    """
    global wake_after_playback

    audio_b64 = tts.speak(reply_text)
    window.evaluate_js(f"addMessage('jarvis', {json.dumps(reply_text)})")
    if not audio_b64:
        return False

    if _is_conversation_end(end_check_text) or _is_conversation_end(reply_text):
        wake_after_playback = "idle"
    else:
        wake_after_playback = "follow_up"
    window.evaluate_js(f"playAudioBase64({json.dumps(audio_b64)})")
    return True


def _process_wake_command(frames_list):
    # Runs after capture ends. While this runs - transcribing, asking
    # Claude, generating speech, and then actually playing the reply -
    # wake_state stays "processing" so the mic is fully ignored. On a
    # successful reply, state moves to "follow_up" (not "idle") only
    # once playback truly finishes - see Api.playback_finished() - so
    # there's a short window to keep talking without repeating the wake
    # word. Every other exit (no audio, empty transcript, or an error)
    # goes straight back to idle instead.
    window.evaluate_js("document.getElementById('mic-btn').classList.remove('recording')")

    handed_off = False  # set True only once handed off to actual playback
    try:
        if not frames_list:
            return

        audio_array = np.concatenate(frames_list, axis=0)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(MIC_SAMPLE_RATE)
            wav_file.writeframes(audio_array.tobytes())

        transcript = tts.transcribe(wav_buffer.getvalue()).strip()
        if not transcript:
            return

        window.evaluate_js(f"addMessage('you', {json.dumps(transcript)})")

        if _is_stand_down(transcript):
            # No reply is generated or spoken - handed_off stays False,
            # so the finally block below sends this straight back to
            # idle instead of into the follow_up window.
            return

        reply = assistant.ask(transcript)
        handed_off = _speak_reply(reply, end_check_text=transcript)
    except Exception:
        print("Error handling wake-word command (recovered):")
        traceback.print_exc()
    finally:
        if not handed_off:
            _enter_idle()

MARGIN_RIGHT = 200  # horizontal gap from the right edge - the icon's position
MARGIN_BOTTOM = 100  # vertical gap from the bottom edge - the icon's position

# Where the chat panel opens the very FIRST time (before it's ever been
# dragged) - independent of the icon's position above, so you can tune
# this without affecting where the idle icon sits.
CHAT_MARGIN_RIGHT = 20
CHAT_MARGIN_BOTTOM = 20

IDLE_WIDTH = 70
IDLE_HEIGHT = 70
EXPANDED_WIDTH = 378  # 280 * 1.35
EXPANDED_HEIGHT = 513  # 380 * 1.35


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
  #input-row {{
    display: flex;
    gap: 6px;
  }}
  #msg-input {{
    flex: 1;
    width: 100%;
    box-sizing: border-box;
    background: #1e1e2e;
    border: 1px solid #2a2a3a;
    color: #E6F1FB;
    border-radius: 8px;
    padding: 8px;
    font-size: 13px;
  }}
  #mic-btn {{
    flex-shrink: 0;
    width: 34px;
    border: 1px solid #2a2a3a;
    border-radius: 8px;
    background: #1e1e2e;
    color: #E6F1FB;
    cursor: pointer;
    font-size: 14px;
  }}
  #mic-btn.recording {{
    background: #a53232;
    border-color: #a53232;
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
  <div id="input-row">
    <input id="msg-input" placeholder="">
    <button id="mic-btn" type="button">&#9679;</button>
  </div>
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

function playAudioBase64(b64) {{
  if (!b64) {{
    window.pywebview.api.playback_finished();
    return;
  }}
  const audio = new Audio("data:audio/mpeg;base64," + b64);
  // Tells Python playback is truly done, not just that a reply was
  // generated - this is what lets the wake-word listener stay paused
  // for as long as Jarvis is actually speaking, so it can't hear (and
  // react to) his own voice through the mic.
  audio.onended = function() {{ window.pywebview.api.playback_finished(); }};
  // Waiting for canplaythrough (fully decoded and ready) before calling
  // play(), rather than playing immediately - starting playback before
  // the browser has finished decoding the data URI was clipping the
  // first word almost entirely.
  audio.oncanplaythrough = function() {{ audio.play(); }};
}}

const msgInput = document.getElementById('msg-input');
msgInput.addEventListener('keydown', function(e) {{
  if (e.key === 'Enter' && msgInput.value.trim() !== '') {{
    const text = msgInput.value.trim();
    msgInput.value = '';
    msgInput.disabled = true;
    addMessage('you', text);
    window.pywebview.api.send_message(text).then(function(result) {{
      addMessage('jarvis', result.text);
      playAudioBase64(result.audio);
      msgInput.disabled = false;
      msgInput.focus();
    }});
  }}
}});

let isRecording = false;
const micBtn = document.getElementById('mic-btn');

micBtn.addEventListener('click', function() {{
  if (!isRecording) {{
    isRecording = true;
    micBtn.classList.add('recording');
    window.pywebview.api.start_recording();
  }} else {{
    isRecording = false;
    micBtn.classList.remove('recording');
    msgInput.disabled = true;
    window.pywebview.api.stop_recording().then(function(result) {{
      if (result.transcript) {{
        addMessage('you', result.transcript);
      }}
      addMessage('jarvis', result.text);
      playAudioBase64(result.audio);
      msgInput.disabled = false;
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


def _open_chat_panel():
    # Shared by the manual click handler and the wake-word listener, so
    # both paths open the panel identically.
    global chat_open
    if chat_open:
        return
    chat_open = True

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

    set_bounds(x, y, width, height)
    window.evaluate_js("setChatOpen(true)")


def _close_chat_panel():
    # Closing ALWAYS returns to the fixed corner - the icon never
    # drifts, regardless of where the panel was dragged.
    global chat_open
    if not chat_open:
        return
    chat_open = False

    width, height = IDLE_WIDTH, IDLE_HEIGHT
    x, y = get_position(IDLE_WIDTH, IDLE_HEIGHT)
    set_bounds(x, y, width, height)
    window.evaluate_js("setChatOpen(false)")


class Api:
    def toggle_chat(self):
        if chat_open:
            _close_chat_panel()
        else:
            _open_chat_panel()

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
        text = assistant.ask(message)
        audio_b64 = tts.speak(text)
        return {"text": text, "audio": audio_b64}

    def playback_finished(self):
        # Called from JS the moment a spoken reply's audio actually
        # finishes playing (see playAudioBase64's audio.onended). Reads
        # wake_after_playback - set in _speak_reply - to decide whether
        # this was an ordinary reply (open the follow_up window) or a
        # conversation-ending one (go straight to idle).
        global wake_after_playback
        next_step = wake_after_playback
        wake_after_playback = "follow_up"
        if next_step == "idle":
            _enter_idle()
        else:
            _enter_follow_up()

    def start_recording(self):
        # Pause the always-on wake-word listener first - both would
        # otherwise try to open the same microphone at once. Then start
        # pulling audio from the default mic in the background;
        # sounddevice calls _on_audio_block() itself as chunks arrive,
        # so there's nothing more to do here than open the stream.
        global recording_stream, recording_frames
        if wake_stream is not None:
            wake_stream.stop()

        recording_frames = []
        recording_stream = sd.InputStream(
            samplerate=MIC_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            callback=_on_audio_block,
        )
        recording_stream.start()

    def stop_recording(self):
        # Stops the mic, stitches the recorded chunks into a proper WAV
        # file in memory, sends it to ElevenLabs to transcribe, then
        # runs the transcript through the exact same ask()/speak()
        # pipeline typed messages use.
        global recording_stream
        if recording_stream is not None:
            recording_stream.stop()
            recording_stream.close()
            recording_stream = None

        if wake_stream is not None:
            _enter_idle()
            wake_stream.start()

        if not recording_frames:
            return {"transcript": "", "text": "Didn't catch anything, sir.", "audio": ""}

        audio_array = np.concatenate(recording_frames, axis=0)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # int16 = 2 bytes per sample
            wav_file.setframerate(MIC_SAMPLE_RATE)
            wav_file.writeframes(audio_array.tobytes())

        transcript = tts.transcribe(wav_buffer.getvalue()).strip()
        if not transcript:
            return {"transcript": "", "text": "Didn't catch that, sir.", "audio": ""}

        reply = assistant.ask(transcript)
        audio_b64 = tts.speak(reply)
        return {"transcript": transcript, "text": reply, "audio": audio_b64}


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

# Starts listening for "Jarvis" immediately, running for the entire
# lifetime of the app in the background - independent of whether the
# chat panel is open or closed.
wake_stream = sd.InputStream(
    samplerate=MIC_SAMPLE_RATE,
    channels=1,
    dtype="int16",
    blocksize=WAKE_CHUNK,
    callback=_wake_word_callback,
)
wake_stream.start()

webview.start()
