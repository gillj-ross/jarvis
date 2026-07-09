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
from collections import deque
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
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

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

# Said any time, these open the chat panel by voice - now that talking
# to him no longer auto-opens it (Jude wants to just speak to the idle
# widget most of the time), this is the deliberate "actually, let me see
# it" escape hatch, alongside the existing click-to-open. Substring
# match, same style as STAND_DOWN_PHRASES below.
CHAT_OPEN_PHRASES = [
    "pull up the chat",
    "pull up chat",
    "open the chat",
    "open chat",
    "show the chat",
    "show chat",
    "bring up the chat",
    "bring up chat",
    "show me the chat",
    "let me see the chat",
    "open the panel",
    "pull up the panel",
    "bring up the panel",
    "open the window",
]


def _is_chat_open_request(transcript: str) -> bool:
    normalized = transcript.lower()
    return any(phrase in normalized for phrase in CHAT_OPEN_PHRASES)


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
    "thanks",
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
# Raised from 2.2 to 3.2 - a full second more breathing room to start
# your next thing before he gives up and goes back to idle.
FOLLOW_UP_SECONDS = 3.2

SILENCE_CHUNKS_NEEDED = int(SILENCE_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)
MIN_COMMAND_CHUNKS = int(MIN_COMMAND_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)
MAX_COMMAND_CHUNKS = int(MAX_COMMAND_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)
FOLLOW_UP_CHUNKS = int(FOLLOW_UP_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)

# openWakeWord only raises the alarm once it's already heard enough of
# "Hey Jarvis" to be confident - by the time _wake_word_callback sees a
# score above WAKE_WORD_THRESHOLD, the actual phrase already happened
# some moments earlier. Continuously keeping the last couple seconds of
# audio around while idle, and handing that whole buffer to the capture
# as soon as the wake word fires, is what lets the wake phrase itself
# end up in the transcript instead of only what's said after it.
IDLE_BUFFER_SECONDS = 2.0
IDLE_BUFFER_CHUNKS = int(IDLE_BUFFER_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)


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
        # Went 2.5x/150 -> 3.5x/250 to fight click false-triggers, but
        # that pushed the bar high enough that normal speaking volume -
        # especially short words - often didn't clear it either, so
        # you'd end up half-shouting for him to hear you. Settling in
        # the middle: still meaningfully above the old click-prone
        # baseline, but not demanding raised-voice levels for it to
        # register as "loud" at all.
        threshold = max(noise_floor * 2.8, 190.0)
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

# Barge-in: lets you talk over Jarvis mid-reply instead of having to wait
# him out, the way Tony does with the real JARVIS. Kept well above
# SILENCE_RMS_THRESHOLD so ambient noise or Jarvis's own voice bleeding
# back in through the mic (a real risk without a headset) doesn't
# trigger nonstop self-interruption - amplitude alone does most of the
# work filtering that out. BARGE_IN_FRAMES_THRESHOLD used to require a
# much longer sustained run (~800ms) on top of that, but that made short,
# urgent interruptions like "Mute" - exactly the case that matters most,
# since it's how Jude actually stops him mid-sentence - fail to register
# at all: a single quick word often doesn't sustain that long. Lowered to
# ~400ms, enough to reject a single-frame click or pop but short enough
# that a deliberately spoken one-word interruption reliably lands. If
# speaker bleed still causes false triggers in practice, a headset
# sidesteps the root cause entirely - worth trying before tuning further.
BARGE_IN_RMS_THRESHOLD = max(SILENCE_RMS_THRESHOLD * 1.8, 350.0)
BARGE_IN_FRAMES_THRESHOLD = 5

# Rolling buffer of recent audio while state == "speaking" - mirrors
# idle_audio_buffer's role for the wake word. Without this, _barge_in()
# only had the ONE chunk that happened to cross the threshold to work
# with, which is somewhere in the MIDDLE of whatever you said - for a
# short word like "Mute," that meant the captured audio often started
# mid-word or later, so it transcribed as noise or a fragment instead of
# a word STAND_DOWN_PHRASES could actually match. Buffering the last
# couple seconds continuously and handing all of it over on trigger
# means the whole word is there for transcription, not just its tail.
BARGE_IN_BUFFER_SECONDS = 1.5
BARGE_IN_BUFFER_CHUNKS = int(BARGE_IN_BUFFER_SECONDS * MIC_SAMPLE_RATE / WAKE_CHUNK)

# The whole listening cycle as one explicit state, rather than a couple
# of overlapping booleans - easier to reason about now that there are
# five distinct phases:
#   idle       - only listening for the wake word itself
#   capturing  - actively recording your command, watching for silence
#   processing - transcribing/thinking - mic fully ignored
#   speaking   - reply audio is actively playing - mic listens ONLY for
#                a sustained barge-in (see BARGE_IN_* above), nothing else
#   follow_up  - reply just finished; listening for you to keep talking
#                without needing to say "Jarvis" again
wake_lock = threading.Lock()
wake_state = "idle"
wake_command_frames = []
wake_silence_run = 0  # consecutive quiet chunks seen since capture started
wake_follow_up_count = 0  # chunks elapsed since entering the follow_up window
wake_loud_run = 0  # consecutive loud chunks seen right now
wake_barge_run = 0  # consecutive loud chunks seen right now, while state == "speaking"
wake_after_playback = "follow_up"  # what to do once the current reply finishes playing
wake_stream = None

# Rolling window of recent audio while idle - see IDLE_BUFFER_SECONDS
# above. Only ever touched from the wake-word callback thread and reset
# in _enter_idle(), so it doesn't need wake_lock the way wake_state does.
idle_audio_buffer = deque(maxlen=IDLE_BUFFER_CHUNKS)

# Same idea, for state == "speaking" - see BARGE_IN_BUFFER_SECONDS above.
barge_in_audio_buffer = deque(maxlen=BARGE_IN_BUFFER_CHUNKS)

# A mouse click, keyboard clack, or similar brief pop is loud enough to
# cross SILENCE_RMS_THRESHOLD for a single 80ms frame, but it isn't
# sustained the way actual speech is. These two thresholds both filter
# that out, but serve different purposes and were splitting them apart
# fixes a real bug: a single one-syllable word ("yes," "stop," "no") -
# exactly what the new confirmation flow depends on - often doesn't
# sustain a full 480ms of loudness, so with one shared 6-frame threshold
# it was never recognized as speech starting during the follow_up
# window at all, and got silently dropped rather than acted on.
#
# ONSET_FRAMES_THRESHOLD gates "did you just start talking" (used in
# follow_up, where missing it means losing the entire reply) - kept
# short so brief words register, at the cost of being a little more
# forgiving of noise.
#
# LOUD_FRAMES_THRESHOLD gates resetting the silence timer during an
# active capture - staying stricter here is safe, since a click mid-
# recording doesn't erase anything (everything still gets recorded
# regardless), it only affects how quickly he decides you've stopped
# talking, so there's no real downside to keeping this one cautious.
ONSET_FRAMES_THRESHOLD = 2
LOUD_FRAMES_THRESHOLD = 6


def _rms(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


def _start_capturing(initial_chunks=None):
    # Common entry point for beginning to record a command, whether
    # triggered by the wake word (from idle - initial_chunks is the
    # rolling idle_audio_buffer, so the wake phrase itself is included)
    # or by you simply talking again during the follow_up window (no
    # wake word needed there - initial_chunks is just that one chunk).
    global wake_state, wake_command_frames, wake_silence_run, wake_loud_run, wake_barge_run
    with wake_lock:
        wake_state = "capturing"
        wake_command_frames = list(initial_chunks) if initial_chunks else []
    wake_silence_run = 0
    wake_loud_run = 0
    wake_barge_run = 0
    # No longer opens the chat panel here - Jude wants to just speak to
    # the idle widget without the panel popping up every time. The idle
    # badge itself now shows the "listening" glow instead (see
    # setListening() in the JS below); the panel only opens by an
    # explicit click or a CHAT_OPEN_PHRASES voice request.
    window.evaluate_js("setListening(true)")


def _enter_idle():
    # Back to square one: only "Jarvis"/"Hey Jarvis" wakes him up again.
    global wake_state, wake_command_frames, wake_silence_run, wake_follow_up_count, wake_loud_run, wake_barge_run
    with wake_lock:
        wake_state = "idle"
        wake_command_frames = []
    wake_silence_run = 0
    wake_follow_up_count = 0
    wake_loud_run = 0
    wake_barge_run = 0
    idle_audio_buffer.clear()
    window.evaluate_js("setListening(false)")


def _enter_follow_up():
    # Called once a reply has actually finished playing out loud (see
    # Api.playback_finished()). Gives you a short window to keep
    # talking without repeating the wake word - if you do, _start_
    # capturing() takes over immediately; if not, _wake_word_callback
    # times it out back to _enter_idle().
    global wake_state, wake_follow_up_count, wake_loud_run, wake_barge_run
    with wake_lock:
        wake_state = "follow_up"
    wake_follow_up_count = 0
    wake_loud_run = 0
    wake_barge_run = 0
    window.evaluate_js("setListening(true)")


def _barge_in():
    # Called from _wake_word_callback the instant sustained loud speech
    # is heard while wake_state == "speaking" - i.e. you're talking over
    # him. Hands over the WHOLE barge_in_audio_buffer, not just the one
    # chunk that crossed the threshold - that chunk is somewhere in the
    # middle of what you said, so using only it would cut off the start
    # of the word (see BARGE_IN_BUFFER_SECONDS above). Moves straight
    # into capturing your new command (same as follow_up's mid-
    # conversation restart) BEFORE telling the page to actually stop the
    # audio, so that by the time playback's onended fires (see
    # stopPlayback() in the JS below), Api.playback_finished() sees a
    # state that's no longer "speaking" and skips its normal follow_up/
    # idle handling instead of clobbering this.
    global wake_barge_run
    wake_barge_run = 0
    _start_capturing(initial_chunks=list(barge_in_audio_buffer))
    window.evaluate_js("stopPlayback()")


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
    global wake_state, wake_command_frames, wake_silence_run, wake_follow_up_count, wake_loud_run, wake_barge_run

    try:
        with wake_lock:
            state = wake_state

        chunk = indata[:, 0].copy()

        if state == "idle":
            idle_audio_buffer.append(chunk)
            prediction = wake_model.predict(chunk)
            if prediction.get(WAKE_WORD_NAME, 0.0) > WAKE_WORD_THRESHOLD:
                # Hand over everything buffered so far - this is what
                # gets the wake phrase itself into the transcript,
                # instead of just what's said after it.
                _start_capturing(initial_chunks=list(idle_audio_buffer))
            return

        if state == "processing":
            # Mic is fully ignored while Jarvis is transcribing or
            # thinking - nothing to interrupt yet, there's no reply
            # playing.
            return

        if state == "speaking":
            # He's actively talking - the ONLY thing being listened for
            # here is a deliberate, sustained talk-over (see BARGE_IN_*
            # above); nothing else about this chunk matters while in
            # this state. Keeps buffering regardless of whether THIS
            # chunk crosses the threshold, so whenever it does trigger,
            # the buffer already has the start of the word in it - see
            # BARGE_IN_BUFFER_SECONDS.
            barge_in_audio_buffer.append(chunk)
            if _rms(chunk) >= BARGE_IN_RMS_THRESHOLD:
                wake_barge_run += 1
            else:
                wake_barge_run = 0
            if wake_barge_run >= BARGE_IN_FRAMES_THRESHOLD:
                _barge_in()
            return

        # Shared loudness tracking for follow_up and capturing below -
        # see ONSET_FRAMES_THRESHOLD/LOUD_FRAMES_THRESHOLD for why these
        # use two different debounce lengths off the same counter.
        if _rms(chunk) >= SILENCE_RMS_THRESHOLD:
            wake_loud_run += 1
        else:
            wake_loud_run = 0
        is_speech_onset = wake_loud_run >= ONSET_FRAMES_THRESHOLD
        is_real_speech = wake_loud_run >= LOUD_FRAMES_THRESHOLD

        if state == "follow_up":
            wake_follow_up_count += 1
            if is_speech_onset:
                # You started talking again - no wake word needed.
                _start_capturing(initial_chunks=[chunk])
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
    global wake_after_playback, wake_state, wake_barge_run

    audio_b64 = tts.speak(reply_text)
    window.evaluate_js(f"addMessage('jarvis', {json.dumps(reply_text)})")
    if not audio_b64:
        return False

    if _is_conversation_end(end_check_text) or _is_conversation_end(reply_text):
        wake_after_playback = "idle"
    else:
        wake_after_playback = "follow_up"
    # Moves from "processing" to "speaking" right as playback starts, so
    # _wake_word_callback starts listening for a barge-in (see BARGE_IN_*
    # above) instead of staying fully deaf for the whole reply.
    with wake_lock:
        wake_state = "speaking"
    wake_barge_run = 0
    barge_in_audio_buffer.clear()
    window.evaluate_js(f"playAudioBase64({json.dumps(audio_b64)})")
    return True


# Signaled by Api.narration_finished() the moment a narration line (see
# _narrate below) actually finishes playing - lets _narrate() block the
# calling thread until Jude's actually heard the heads-up before the
# tool call it's announcing goes ahead, rather than talking over itself.
narration_done = threading.Event()


def _narrate(text):
    """
    Speaks a short "doing this now" line and waits for it to actually
    finish playing before returning - passed into assistant.ask() as
    on_narrate, called just before a slower, non-risky tool call (a
    shell command, writing a file) so there's something to listen to
    instead of dead air while it runs. Deliberately routed through a
    SEPARATE pywebview callback (narration_finished, not
    playback_finished) so it doesn't trip the normal reply-finished
    logic (opening follow_up, going idle, etc.) - a narration line isn't
    the actual reply, just a heads-up before more work happens.
    """
    audio_b64 = tts.speak(text)
    window.evaluate_js(f"addMessage('jarvis', {json.dumps(text)})")
    if not audio_b64:
        return
    narration_done.clear()
    window.evaluate_js(f"playAudioBase64({json.dumps(audio_b64)}, true)")
    # Timeout is just a safety net in case the JS callback never fires
    # for some reason - shouldn't ever actually hit it in practice.
    narration_done.wait(timeout=10)


def _process_wake_command(frames_list):
    # Runs after capture ends. While this runs - transcribing, asking
    # Claude, generating speech, and then actually playing the reply -
    # wake_state stays "processing" so the mic is fully ignored. On a
    # successful reply, state moves to "follow_up" (not "idle") only
    # once playback truly finishes - see Api.playback_finished() - so
    # there's a short window to keep talking without repeating the wake
    # word. Every other exit (no audio, empty transcript, or an error)
    # goes straight back to idle instead.
    window.evaluate_js("setListening(false)")

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

        if _is_chat_open_request(transcript):
            # Deliberate "let me see it" request - open the panel and
            # give a short spoken acknowledgment, but skip sending this
            # through assistant.ask() entirely, same as stand-down below.
            _open_chat_panel()
            handed_off = _speak_reply("There you go, sir.", end_check_text=transcript)
            return

        if _is_stand_down(transcript):
            # No reply is generated or spoken - handed_off stays False,
            # so the finally block below sends this straight back to
            # idle instead of into the follow_up window.
            return

        reply = assistant.ask(transcript, on_narrate=_narrate)
        handed_off = _speak_reply(reply, end_check_text=transcript)
    except Exception:
        print("Error handling wake-word command (recovered):")
        traceback.print_exc()
    finally:
        if not handed_off:
            _enter_idle()

MARGIN_RIGHT = 200  # horizontal gap from the right edge - only used for the very first launch position
MARGIN_BOTTOM = 100  # vertical gap from the bottom edge - only used for the very first launch position

# Shrunk from 70x70 - a smaller idle footprint that's less likely to
# sit over anything you're actually working in.
IDLE_WIDTH = 44
IDLE_HEIGHT = 44
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


def _set_topmost(topmost: bool):
    """
    Toggles always-on-top at runtime, separate from position/size.
    Staying topmost permanently was exactly what made the idle icon
    bother Jude while working in other apps - now it's only forced above
    everything else while the chat panel is actually open (so it can't
    get buried mid-conversation), and drops back to normal z-order the
    moment it closes.
    """
    hwnd = ctypes.windll.user32.FindWindowW(None, "Jarvis")
    insert_after = HWND_TOPMOST if topmost else HWND_NOTOPMOST
    ctypes.windll.user32.SetWindowPos(
        hwnd, insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
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
  #badge .orb-svg {{ width: 32px; height: 32px; }}
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
  /* Now that the panel usually stays hidden (see setListening() below),
     the idle badge itself needs to show when he's actively listening -
     a faster pulse and a warmer glow, distinct from the resting blue. */
  @keyframes atomGlowActive {{
    0%, 100% {{ filter: drop-shadow(0 0 8px rgba(165,50,50,0.8)); }}
    50% {{ filter: drop-shadow(0 0 16px rgba(220,90,90,1)); }}
  }}
  #badge.recording .orb-svg {{ animation: atomGlowActive 1s ease-in-out infinite; }}
</style>
</head>
<body>
<div id="badge" class="pywebview-drag-region">
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

// Toggles the "actively listening" indicator on BOTH the idle badge and
// the mic button - only one of the two is ever visible at a time, but
// this way whichever one you're looking at shows the right state
// without the callers needing to know or care which is currently shown.
function setListening(active) {{
  document.getElementById('badge').classList.toggle('recording', active);
  document.getElementById('mic-btn').classList.toggle('recording', active);
}}

function addMessage(sender, text) {{
  const messages = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg ' + sender;
  div.textContent = text;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}}

// One AudioContext, created once and reused for every reply for the
// life of the widget, rather than a fresh <audio> element each time.
// Waiting for canplaythrough on a brand-new <audio> element still
// clipped the first syllable on EVERY reply, not just a cold start -
// each new element has to stand up its own connection to the output
// device from scratch, and that handshake was eating real audio before
// playback actually began. Reusing one context keeps that connection
// warm across the whole session, and decodeAudioData() only resolves
// once the audio is fully decoded into raw samples - a stronger
// guarantee than canplaythrough - so scheduling playback after that is
// as tight as this can get.
let audioCtx = null;

// Playback volume multiplier - 1.0 is unchanged, 1.3 is 30% louder.
// Applied via a GainNode rather than touching the TTS audio itself, so
// it's a pure playback-side boost with no re-encoding involved.
const VOLUME_GAIN = 1.3;

// The currently-playing source node, if any - tracked so a barge-in
// (see _barge_in in the Python) can force-stop playback immediately via
// stopPlayback() below, rather than having to wait for it to finish on
// its own.
let currentSource = null;

function stopPlayback() {{
  if (currentSource) {{
    try {{ currentSource.stop(); }} catch (e) {{}}
    currentSource = null;
  }}
}}

// isNarration distinguishes a short "doing this now" heads-up (see
// _narrate in the Python) from an actual reply: both play through the
// exact same pipeline, but a narration line reports back through a
// separate callback (narration_finished) so it doesn't trigger the
// normal reply-finished handling (opening follow_up, going idle, etc.)
// meant for actual replies.
function playAudioBase64(b64, isNarration) {{
  const notify = function() {{
    if (isNarration) {{ window.pywebview.api.narration_finished(); }}
    else {{ window.pywebview.api.playback_finished(); }}
  }};
  if (!b64) {{
    notify();
    return;
  }}
  if (!audioCtx) {{
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }}
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {{
    bytes[i] = binary.charCodeAt(i);
  }}
  audioCtx.decodeAudioData(bytes.buffer).then(function(audioBuffer) {{
    const source = audioCtx.createBufferSource();
    source.buffer = audioBuffer;
    const gainNode = audioCtx.createGain();
    gainNode.gain.value = VOLUME_GAIN;
    source.connect(gainNode);
    gainNode.connect(audioCtx.destination);
    currentSource = source;
    // Tells Python playback is truly done, not just that a reply was
    // generated - this is what lets the wake-word listener stay paused
    // for as long as Jarvis is actually speaking, so it can't hear (and
    // react to) his own voice through the mic. Also fires if stopPlayback()
    // force-stops this early (a barge-in) - .stop() triggers 'ended' too.
    source.onended = function() {{ currentSource = null; notify(); }};
    return audioCtx.resume().then(function() {{
      source.start();
    }});
  }}).catch(function(err) {{
    console.error("Playback failed:", err);
    notify();
  }});
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
    setListening(true);
    window.pywebview.api.start_recording();
  }} else {{
    isRecording = false;
    setListening(false);
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

# The ONE shared position the idle icon and the expanded chat panel are
# both anchored to - specifically, their shared BOTTOM-RIGHT corner
# (the corner that stays fixed while the panel grows "up and to the
# left" from the icon - see get_position()). Used to be two separate
# remembered spots (one for the icon, one for the panel), which meant
# moving one didn't affect the other, so opening/closing could jump
# between two unrelated positions. Now there's exactly one anchor:
# dragging either the icon OR the open panel updates the same point,
# and both derive their own top-left corner from it.
last_anchor = None


def _open_chat_panel():
    # Shared by the manual click handler and the wake-word listener, so
    # both paths open the panel identically.
    global chat_open, last_anchor
    if chat_open:
        return
    chat_open = True

    if last_anchor is None:
        # Never moved yet - anchor off wherever the icon actually is
        # right now, so even the very first open lines up with it
        # instead of jumping to a separately hardcoded default spot.
        rect = get_current_rect()
        last_anchor = (rect.left + IDLE_WIDTH, rect.top + IDLE_HEIGHT)

    anchor_x, anchor_y = last_anchor
    x, y = anchor_x - EXPANDED_WIDTH, anchor_y - EXPANDED_HEIGHT
    set_bounds(x, y, EXPANDED_WIDTH, EXPANDED_HEIGHT)
    # Topmost only while actually open, so it can't get buried behind
    # whatever you just clicked into mid-conversation - see _set_topmost.
    _set_topmost(True)
    window.evaluate_js("setChatOpen(true)")


def _close_chat_panel():
    # Returns the icon to the same shared anchor the panel was just
    # occupying, so it reappears exactly where the panel's corner was.
    global chat_open
    if not chat_open:
        return
    chat_open = False

    anchor_x, anchor_y = last_anchor
    x, y = anchor_x - IDLE_WIDTH, anchor_y - IDLE_HEIGHT
    set_bounds(x, y, IDLE_WIDTH, IDLE_HEIGHT)
    # Drop back to normal z-order now that nothing's actively open -
    # this is the piece that stops the idle icon from sitting on top of
    # whatever else you're working in.
    _set_topmost(False)
    window.evaluate_js("setChatOpen(false)")


class Api:
    def toggle_chat(self):
        if chat_open:
            _close_chat_panel()
        else:
            _open_chat_panel()

    def save_position(self):
        # Works in both states - dragging the idle icon OR the open
        # chat panel both update the same shared anchor point.
        global last_anchor
        rect = get_current_rect()
        width, height = (EXPANDED_WIDTH, EXPANDED_HEIGHT) if chat_open else (IDLE_WIDTH, IDLE_HEIGHT)
        last_anchor = (rect.left + width, rect.top + height)

    def send_message(self, message):
        # Same assistant.ask() the CLI (main.py) uses - same memory
        # database too, since both point at the same memory.py file.
        text = assistant.ask(message, on_narrate=_narrate)
        audio_b64 = tts.speak(text)
        return {"text": text, "audio": audio_b64}

    def playback_finished(self):
        # Called from JS the moment a spoken reply's audio actually
        # finishes playing (see playAudioBase64's audio.onended) - this
        # also fires when a barge-in force-stops playback early (see
        # _barge_in/stopPlayback), not just on a natural end. Reads
        # wake_after_playback - set in _speak_reply - to decide whether
        # this was an ordinary reply (open the follow_up window) or a
        # conversation-ending one (go straight to idle).
        global wake_after_playback
        next_step = wake_after_playback
        wake_after_playback = "follow_up"
        with wake_lock:
            current_state = wake_state
        if current_state != "speaking":
            # A barge-in already moved things on to "capturing" (or
            # something else beat this to it) - don't clobber that by
            # jumping to follow_up/idle now.
            return
        if next_step == "idle":
            _enter_idle()
        else:
            _enter_follow_up()

    def narration_finished(self):
        # Called from JS the moment a narration line (see _narrate)
        # finishes playing - separate from playback_finished() above so
        # a mid-task narration never triggers the actual reply-finished
        # transitions (follow_up/idle).
        narration_done.set()

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

        reply = assistant.ask(transcript, on_narrate=_narrate)
        audio_b64 = tts.speak(reply)
        return {"transcript": transcript, "text": reply, "audio": audio_b64}


window = webview.create_window(
    "Jarvis",
    html=widget_html,
    width=IDLE_WIDTH,
    height=IDLE_HEIGHT,
    frameless=True,
    easy_drag=False,
    # Starts NOT topmost - idle is the default, most-of-the-time state,
    # and forcing it above every other window was exactly what made it
    # bother Jude while working elsewhere. _open_chat_panel() switches
    # this on for as long as the chat panel is actually open.
    on_top=False,
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
