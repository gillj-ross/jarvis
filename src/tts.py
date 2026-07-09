"""
tts.py

Converts Jarvis's text replies into speech using ElevenLabs, and hands
back the audio as base64 - a plain text format that can be embedded
directly into an HTML <audio> tag via a data: URL, so the widget can
play it without ever saving a temp file to disk.
"""

import base64
import io
import os
import re

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()

client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# A default starter voice from ElevenLabs' library. Swap this for any
# voice ID you like - browse elevenlabs.io/app/voice-library, click into
# a voice you like, and copy its Voice ID from there.
VOICE_ID = "oQ3jHNJpnC1H30u4KKgJ"

# Controls HOW the voice delivers a line, separate from which voice it
# is.
# stability: 0.0-1.0, lower = more expressive/varied delivery, higher =
#   more consistent and even, but can sound flatter.
# similarity_boost: 0.0-1.0, how closely it sticks to the original
#   voice's exact character - higher can sound more "authentic" but
#   sometimes more intense/sharp too. Lower it for a gentler tone.
# style: 0.0-1.0, how much it exaggerates the voice's natural style -
#   0 is the flattest, most neutral, softest delivery; higher gets
#   more dramatic/emphatic.
# use_speaker_boost: boosts clarity and presence - True can sound
#   sharper/louder in tone; False is gentler and more natural.
# speed: 0.7-1.2, playback pace - 1.0 is normal, lower is slower/more
#   deliberate. Good lever for a calmer, more measured delivery.
# Tweak these and see what sounds right - there's no universally
# "correct" value, it's a matter of taste.
VOICE_SETTINGS = {
    "stability": 0.6,
    "similarity_boost": 0.7,
    "style": 0.15,
    "use_speaker_boost": False,
    "speed": 0.95,
}

# Matches a sentence-ending punctuation mark followed by whitespace, so
# we can insert a pause there.
SENTENCE_END = re.compile(r"([.!?])\s+")


def _add_pacing(text: str) -> str:
    """
    ElevenLabs treats a line break as a natural pause - without this,
    it reads a multi-sentence reply as one continuous rushed stream.
    Inserting one after every sentence gives it a beat to breathe
    between them, closer to normal speech. Deliberately a single line
    break rather than a full blank-line paragraph break: that was
    adding up to a very noticeable drag on longer, multi-sentence
    replies, since every extra sentence meant another full pause
    stacked on top of the last.
    """
    return SENTENCE_END.sub(r"\1\n", text)


def speak(text: str) -> str:
    """
    Converts text to speech and returns the audio as a base64 string.
    """
    audio_stream = client.text_to_speech.convert(
        text=_add_pacing(text),
        voice_id=VOICE_ID,
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
        voice_settings=VOICE_SETTINGS,
    )
    # convert() returns the audio in chunks (a generator), even when
    # we want the whole thing at once - join them into one bytes object
    # before encoding.
    audio_bytes = b"".join(audio_stream)
    return base64.b64encode(audio_bytes).decode("utf-8")


def transcribe(wav_bytes: bytes) -> str:
    """
    The mirror image of speak(): turns recorded audio (raw WAV bytes)
    into text, using ElevenLabs' Scribe speech-to-text model. Same
    account, same client, so voice works both directions through one
    provider.
    """
    audio_file = io.BytesIO(wav_bytes)
    audio_file.name = "recording.wav"  # some multipart clients need a filename hint
    result = client.speech_to_text.convert(
        file=audio_file,
        model_id="scribe_v2",
        # Scribe defaults to annotating non-speech sounds (clicks, taps,
        # etc.) directly in the text, e.g. "(clicking)" - that was
        # literally showing up in the transcript and getting sent to
        # Claude as if it were something you said. Turning it off means
        # only actual spoken words end up in the transcript.
        tag_audio_events=False,
    )
    return result.text
