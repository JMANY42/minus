"""What a setting can be set to, where the answer is a short list.

Six fields in config.py name something out of a set rather than taking free
text: the three model names, the TTS voice, and the two speech-recognition
models. Typing those from memory is how `am_puck` becomes `am_puk` and the
assistant comes back up mute, so the management panel offers them as a menu.

Held here rather than asked for over the socket. The dashboard is opened at
least as often with MINUS stopped as with it running -- to read the log that
says why -- and a menu that stayed empty until the assistant came back would
be empty at exactly the moment someone is trying to change what it starts with.

Where each list came from, so it can be re-derived rather than guessed at when
it ages:

  * VOICES -- every key in models/voices-v1.0.bin, which is the file
    KokoroSpeaker loads and therefore the whole of what it can say anything in.
  * STT_MODELS -- faster_whisper.utils._MODELS, which is what RealtimeSTT
    validates `model` and `realtime_model_type` against.
  * MODELS -- the two MINUS ships with plus the three that were asked for,
    each checked against https://openrouter.ai/api/v1/models on 2026-08-13.

None of the three is a fence. Every menu ends in OTHER, which opens the
free-text editor behind it: OpenRouter carries four hundred models and
faster-whisper will take a path to a local one, so a list that could only be
chosen from would be a smaller dashboard than the one that just took typing.
"""

from __future__ import annotations

# The last row of every menu. Not a value -- the panel treats it as "let me
# type one instead" -- so it is spelled as an instruction rather than as
# something that could be mistaken for a model name.
OTHER = "type another value…"

MODELS = (
    # What MINUS ships with. Both carry `:nitro`, OpenRouter's throughput
    # routing, which is chosen for latency and not for depth -- see config.py.
    "openai/gpt-oss-20b:nitro",
    "deepseek/deepseek-v4-flash-0731:nitro",
    # The step up. assembly.py already names claude-opus-5 as where the deep
    # tier goes if the sparse-MoE default proves too weak. No `:nitro` on
    # these three: it routes between providers, and there is one provider.
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-5",
    "meta-llama/llama-4-maverick",
)

# Sorted, which groups them by the prefix Kokoro encodes language and gender
# in: af_/am_ American, bf_/bm_ British, then Spanish, French, Hindi, Italian,
# Japanese, Portuguese and Chinese. The English ones are what MINUS is prompted
# in, and they come first for that reason rather than by accident.
VOICES = (
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
)

# Smallest first, so that the top of the menu is the end that a realtime model
# is picked from and the bottom is the end that accuracy is picked from. The
# `.en` variants are the English-only weights: smaller and better at the one
# language MINUS is prompted in.
STT_MODELS = (
    "tiny.en",
    "tiny",
    "base.en",
    "base",
    "distil-small.en",
    "small.en",
    "small",
    "distil-medium.en",
    "medium.en",
    "medium",
    "distil-large-v2",
    "distil-large-v3",
    "distil-large-v3.5",
    "large-v1",
    "large-v2",
    "large-v3",
    "large-v3-turbo",
    "turbo",
    "large",
)

# Which fields get a menu, and which list. A field absent from here takes
# typing, which is the right answer for a threshold or a timeout: there is no
# set of sensible values for those, only a range.
CHOICES: dict[str, tuple[str, ...]] = {
    "chat_model": MODELS,
    "fact_extraction_model": MODELS,
    "deep_model": MODELS,
    "tts_voice": VOICES,
    "stt_model": STT_MODELS,
    "stt_realtime_model": STT_MODELS,
}


def choices_for(name: str) -> tuple[str, ...]:
    """The menu for a setting, or nothing if it takes free text."""
    return CHOICES.get(name, ())
