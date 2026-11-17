import asyncio, subprocess, sys
sys.path.insert(0, r"D:\param shavak\project KS\munimai")
sys.stdout.reconfigure(encoding="utf-8")
import edge_tts, imageio_ffmpeg
import backend.config
from backend.bhashini_client import BhashiniClient

FF = imageio_ffmpeg.get_ffmpeg_exe()
bh = BhashiniClient()
phrases = [
    "ನನಗೆ ಮುದ್ರಾ ಸಾಲ ಸಿಗುತ್ತದೆಯೇ?",
    "ನನ್ನ ಹೊಲಿಗೆ ಅಂಗಡಿಗೆ ಮುದ್ರಾ ಸಾಲ ಸಿಗಬಹುದೇ?",
    "ಮುದ್ರಾ ಸಾಲಕ್ಕೆ ಯಾರು ಅರ್ಜಿ ಹಾಕಬಹುದು?",
]


async def tts(text, voice, out):
    await edge_tts.Communicate(text, voice).save(out)


for i, ph in enumerate(phrases):
    for voice in ("kn-IN-SapnaNeural", "kn-IN-GaganNeural"):
        mp3, wav = f"p{i}.mp3", f"p{i}_{voice[-10:-6]}.wav"
        asyncio.run(tts(ph, voice, mp3))
        subprocess.run([FF, "-y", "-v", "error", "-i", mp3, "-ar", "16000", "-ac", "1", wav], check=True)
        try:
            heard = bh.speech_to_text(open(wav, "rb").read(), "kn")
            en = bh.translate(heard, "kn", "en")
        except Exception as e:
            heard, en = "ERR " + str(e)[:80], ""
        print(i, voice[6:11], "|", heard, "|", en)
