"""
Records the SaralcreditAI launch video from the real running app.

1. Narration is synthesised first (free neural voices via edge-tts), so every
   scene's length is known.
2. A scripted browser take drives the real app (real Qwen, Bhashini, documents,
   PDFs). The Kannada question is played into the page as a fake microphone, so
   the voice feature is genuinely exercised. Captions, a visible cursor, title
   cards and PDF previews are injected as overlays.
3. ffmpeg mixes the narration and the app's own spoken replies and encodes MP4.
"""
import asyncio
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import edge_tts
import imageio_ffmpeg
import pymupdf
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"; OUT.mkdir(exist_ok=True)
ASSETS = HERE / "assets"; ASSETS.mkdir(exist_ok=True)
FF = imageio_ffmpeg.get_ffmpeg_exe()
BASE = "http://127.0.0.1:8088"
NARR_VOICE = "en-IN-NeerjaNeural"

NARR = {
 "intro": "Millions of small business owners in India qualify for government loans and schemes, and never find out. This is SaralcreditAI: a credit and compliance assistant that speaks your language.",
 "setup": "It starts with your own business profile. No demo owner, and no long sign-up forms.",
 "voice": "Ask out loud, in your own language. Here a tailor asks in Kannada whether she can get a Mudra loan for her sewing shop.",
 "steps": "Watch the steps on the right. It translates, checks the official rules, writes the reply in Kannada, and speaks it back.",
 "scheme": "It knows far more than Mudra. PMEGP, CGTMSE, Startup India and other government schemes. Every answer quotes the official clause, and never mixes schemes up.",
 "bills": "Drop in bills as photos or PDFs. It reads the amount, the GST and who paid whom, and adds them to your books. The arithmetic is done in code, not guessed.",
 "doc": "Upload a certificate, then just ask about it. Your Udyam number is read from your own document, and ID numbers like Aadhaar stay masked.",
 "web": "When the official pages do not cover a question, it searches the web, and says plainly that the source is not official.",
 "export": "Any answer can be saved as a PDF or an image, in your language.",
 "bank": "Then the part banks care about. One tap builds a bank file: income, GST, scheme fit with citations, and a checklist of missing proofs.",
 "app": "And it drafts the loan application and the covering letter to your branch manager. Missing facts stay as blanks, and nothing is submitted for you.",
 "trace": "Every step is visible: which agent ran, which model, and how long it took. A small helper model reads documents on ordinary memory, while the main model stays within seventeen gigabytes of graphics memory.",
 "outro": "SaralcreditAI. Compliance is how a small business becomes bankable. Free, open source, and built for Bharat.",
}
CAPS = {
 "intro": "Millions of small businesses qualify for loans and schemes, and never find out.",
 "setup": "Your own business profile. No demo owner.",
 "voice": "Ask out loud, in Kannada.",
 "steps": "Translate, check official rules, reply in Kannada, speak it back.",
 "scheme": "PMEGP, CGTMSE, Startup India and more, always with the official clause.",
 "bills": "Bills become books. GST arithmetic is done in code.",
 "doc": "Ask about your own documents. Aadhaar and PAN stay masked.",
 "web": "No official answer? It searches the web and labels the source.",
 "export": "Save any answer as a PDF or an image.",
 "bank": "One tap: a bank-ready file with proofs and gaps.",
 "app": "Loan application and branch letter. Nothing is submitted for you.",
 "trace": "Every step visible. Main model under 17 GB of GPU memory.",
 "outro": "Compliance is how a small business becomes bankable.",
}


def run(*a):
    subprocess.run([FF, "-y", "-v", "error", *a], check=True)


def duration(path: Path) -> float:
    r = subprocess.run([FF, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


# --------------------------------------------------------------- assets
def build_assets():
    async def tts(text, voice, out):
        await edge_tts.Communicate(text, voice, rate="-4%").save(str(out))

    nar = {}
    for k, text in NARR.items():
        p = ASSETS / f"n_{k}.mp3"
        if not p.exists():
            asyncio.run(tts(text, NARR_VOICE, p))
        nar[k] = (p, duration(p))
    # the spoken Kannada question, padded so the app's silence detector fires
    q = ASSETS / "q.mp3"
    if not q.exists():
        asyncio.run(tts("ನನ್ನ ಹೊಲಿಗೆ ಅಂಗಡಿಗೆ ಮುದ್ರಾ ಸಾಲ ಸಿಗಬಹುದೇ?", "kn-IN-SapnaNeural", q))
    run("-i", str(q), "-af", "adelay=700|700,apad=pad_dur=4", "-ar", "16000", "-ac", "1", str(ASSETS / "mic.wav"))

    def pdf(name, lines):
        d = pymupdf.open(); p = d.new_page()
        p.insert_text((50, 70), "\n".join(lines), fontsize=11)
        d.save(str(ASSETS / name))
    pdf("bill1.pdf", ["TAX INVOICE", "Seller: Mysore Silk Traders, Mysuru", "Bill to: Anita Tailors", "Silk cloth 10 metres  5000.00",
                      "CGST 6%  300.00", "SGST 6%  300.00", "Total payable  5600.00"])
    pdf("bill2.pdf", ["TAX INVOICE", "Seller: Anita Tailors, Mysuru", "Bill to: Lakshmi Boutique", "Blouse stitching 24 pieces  14000.00",
                      "CGST 2.5%  350.00", "SGST 2.5%  350.00", "Total payable  14700.00"])
    pdf("bill3.pdf", ["TAX INVOICE", "Seller: Shree Thread House, Hubballi", "Bill to: Anita Tailors", "Thread and buttons  2000.00",
                      "IGST 12%  240.00", "Total payable  2240.00"])
    pdf("udyam.pdf", ["UDYAM REGISTRATION CERTIFICATE", "Udyam Registration Number: UDYAM-KA-18-0012345",
                      "Name of Enterprise: Anita Tailors", "Type of Enterprise: Micro", "Major Activity: Manufacturing",
                      "Date of Incorporation: 04/02/2021", "Official address: 12 Market Road, Mysuru, Karnataka"])
    return nar


# ------------------------------------------------------------- overlays
INIT = r"""
(() => {
  const css = document.createElement('style');
  css.textContent = `
  #__cap{position:fixed;top:74px;left:50%;transform:translateX(-50%);z-index:99998;max-width:760px;padding:12px 22px;
    background:rgba(7,90,87,.94);color:#fff;font:600 21px/1.35 'Noto Sans',system-ui,sans-serif;border-radius:14px;
    box-shadow:0 8px 30px #0006;text-align:center;opacity:0;transition:opacity .3s;pointer-events:none}
  #__cap.on{opacity:1}
  #__cur{position:fixed;z-index:99999;width:26px;height:26px;pointer-events:none;left:-50px;top:-50px;
    transition:left .04s linear,top .04s linear}
  #__cur svg{filter:drop-shadow(0 2px 3px #0008)}
  .__rip{position:fixed;z-index:99997;width:14px;height:14px;border-radius:50%;border:3px solid #F5A524;pointer-events:none;
    animation:__rip .6s ease-out forwards}
  @keyframes __rip{to{transform:scale(4.5);opacity:0}}
  #__card{position:fixed;inset:0;z-index:100000;display:none;flex-direction:column;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#075A57,#0B7A75 55%,#F5A524);color:#fff;text-align:center;padding:40px;font-family:'Bricolage Grotesque','Noto Sans',sans-serif}
  #__card h1{font-size:76px;margin:0 0 14px;letter-spacing:-2px} #__card p{font-size:28px;max-width:880px;margin:0;opacity:.95;line-height:1.35}
  #__card small{margin-top:26px;font-size:20px;opacity:.85}
  #__lb{position:fixed;inset:0;z-index:99990;display:none;background:rgba(14,42,51,.82);align-items:center;justify-content:center;gap:22px;flex-wrap:nowrap;padding-top:60px}
  #__lb img{height:600px;border-radius:8px;box-shadow:0 12px 40px #0008;background:#fff}
  #__lbl{position:fixed;z-index:99991;top:22px;left:50%;transform:translateX(-50%);color:#fff;font:600 26px 'Noto Sans';display:none;
    background:#075A57;padding:8px 20px;border-radius:999px}
  `;
  document.addEventListener('DOMContentLoaded', () => {
    document.head.appendChild(css);
    document.body.insertAdjacentHTML('beforeend',
      '<div id="__cap"></div><div id="__cur"><svg width="26" height="26" viewBox="0 0 24 24"><path d="M3 2l7 19 3-8 8-3z" fill="#fff" stroke="#0E2A33" stroke-width="1.5"/></svg></div><div id="__card"></div><div id="__lb"></div><div id="__lbl"></div>');
    document.addEventListener('mousemove', e => { const c = document.getElementById('__cur'); c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; });
    document.addEventListener('mousedown', e => { const r = document.createElement('div'); r.className = '__rip';
      r.style.left = (e.clientX - 7) + 'px'; r.style.top = (e.clientY - 7) + 'px'; document.body.appendChild(r); setTimeout(() => r.remove(), 700); });
  });
  window.__cap = t => { const c = document.getElementById('__cap'); if (!t) { c.classList.remove('on'); return; } c.textContent = t; c.classList.add('on'); };
  window.__card = (h, p, s) => { const c = document.getElementById('__card'); c.innerHTML = '<h1>' + h + '</h1><p>' + p + '</p>' + (s ? '<small>' + s + '</small>' : ''); c.style.display = 'flex'; };
  window.__cardOff = () => { document.getElementById('__card').style.display = 'none'; };
  window.__lb = (imgs, label) => { const l = document.getElementById('__lb'); l.innerHTML = imgs.map(s => '<img src="' + s + '">').join(''); l.style.display = 'flex';
    const b = document.getElementById('__lbl'); b.textContent = label; b.style.display = 'block'; };
  window.__lbOff = () => { document.getElementById('__lb').style.display = 'none'; document.getElementById('__lbl').style.display = 'none'; };
  // record the app's own spoken-reply clips so they can be mixed into the soundtrack
  window.__clips = [];
  const A = window.Audio;
  window.Audio = function (src) { const a = new A(src); if (src && src.startsWith('data:audio')) window.__clips.push({t: performance.now(), src}); return a; };
})();
"""


def main():
    nar = build_assets()
    print({k: round(v[1], 1) for k, v in nar.items()})
    mix = []          # (start_seconds, path)
    log = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={ASSETS / 'mic.wav'}%noloop", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(OUT),
                                  record_video_size={"width": 1280, "height": 720}, permissions=["microphone"])
        ctx.add_init_script(INIT)
        page = ctx.new_page()
        t0 = time.time()
        now = lambda: time.time() - t0

        def say(key):
            mix.append((now(), nar[key][0]))
            page.evaluate("t => window.__cap(t)", CAPS[key])
            return now(), nar[key][1]

        def finish(start, dur, extra=0.7):
            left = start + dur + extra - now()
            if left > 0:
                time.sleep(left)
            page.evaluate("window.__cap('')")

        def move_to(sel, steps=28):
            page.locator(sel).first.scroll_into_view_if_needed(); time.sleep(0.25)
            box = page.locator(sel).first.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
            time.sleep(0.2)

        def click(sel):
            move_to(sel); page.mouse.down(); time.sleep(0.08); page.mouse.up(); time.sleep(0.3)

        def idle(timeout=150000):
            page.wait_for_function("document.getElementById('orb').dataset.s==='idle'", timeout=timeout)

        def ask(text):
            click("#msg"); page.keyboard.type(text, delay=38); time.sleep(0.3)
            page.keyboard.press("Enter")
            page.wait_for_function("document.getElementById('orb').dataset.s!=='idle'", timeout=15000)
            idle()

        def fetch_pdf_pages(href, n=2, dpi=80):
            data = page.request.get(BASE + href).body()
            d = pymupdf.open(stream=data, filetype="pdf")
            imgs = [ "data:image/png;base64," + base64.b64encode(d[i].get_pixmap(dpi=dpi).tobytes("png")).decode()
                     for i in range(min(n, len(d)))]
            return imgs, len(d)

        page.goto(BASE + "/sim/")
        page.wait_for_selector("#setup.on", timeout=20000)
        nav_t = now()

        # 1 intro ------------------------------------------------------------
        s, d = say("intro")
        page.evaluate("window.__card('SaralcreditAI', 'Loans, schemes and paperwork for small businesses, in your language.', 'Kannada · Hindi · English · voice and text')")
        finish(s, d, 0.4); page.evaluate("window.__cardOff()")

        # 2 onboarding -------------------------------------------------------
        s, d = say("setup")
        click("#bName"); page.keyboard.type("Anita Tailors", delay=60); time.sleep(0.4)
        click("#setupSave"); time.sleep(0.6)
        finish(s, d)

        # 3 voice (Kannada) --------------------------------------------------
        s, d = say("voice")
        time.sleep(1.5)
        click("#orb")
        page.wait_for_function("document.getElementById('orb').dataset.s==='listening'", timeout=15000)
        page.wait_for_function("document.getElementById('orb').dataset.s==='thinking'", timeout=40000)
        finish(s, d, 0.2)
        s2, d2 = say("steps")
        page.wait_for_selector(".msg.them .bubble:not(:has(.dots))", timeout=120000)
        finish(s2, d2, 0.2)
        # let the spoken reply play out, then record when its clips began
        page.wait_for_function("document.getElementById('orb').dataset.s==='speaking'", timeout=60000)
        try:
            idle(timeout=70000)
        except Exception:  # very long spoken reply: stop it like a user would
            click("#orb")
        clips = page.evaluate("window.__clips")
        for c in clips:
            wav = ASSETS / f"clip_{len(mix)}.wav"
            wav.write_bytes(base64.b64decode(c["src"].split(",", 1)[1]))
            mix.append((nav_t + c["t"] / 1000.0, wav))
        time.sleep(1.0)

        # 4 schemes ----------------------------------------------------------
        s, d = say("scheme")
        time.sleep(0.8)
        ask("Am I eligible for PMEGP and what is the maximum project cost for manufacturing?")
        finish(s, d)

        # 5 bills ------------------------------------------------------------
        s, d = say("bills")
        move_to("#attach"); time.sleep(0.3)
        page.set_input_files("#file", [str(ASSETS / "bill1.pdf"), str(ASSETS / "bill2.pdf"), str(ASSETS / "bill3.pdf")])
        page.wait_for_function("document.querySelectorAll('#books .entry').length>=3", timeout=240000)
        finish(s, d)

        # 6 documents --------------------------------------------------------
        s, d = say("doc")
        page.set_input_files("#file", str(ASSETS / "udyam.pdf"))
        page.wait_for_selector("#docs .doc", timeout=120000)
        time.sleep(1.5)
        ask("What is my Udyam registration number and what type of enterprise am I?")
        finish(s, d)

        # 7 web --------------------------------------------------------------
        s, d = say("web")
        ask("What is the current RBI repo rate?")
        finish(s, d)

        # 8 export -----------------------------------------------------------
        s, d = say("export")
        btn = ".save button[data-f=pdf]"
        page.locator(btn).last.scroll_into_view_if_needed(); time.sleep(0.4)
        box = page.locator(btn).last.bounding_box()
        page.mouse.move(box["x"] + 20, box["y"] + 14, steps=26); time.sleep(0.2); page.mouse.down(); page.mouse.up()
        page.wait_for_selector("a.chip", timeout=30000)
        href = page.locator("a.chip").last.get_attribute("href")
        imgs, n = fetch_pdf_pages(href, 1)
        page.evaluate("([i,l]) => window.__lb(i,l)", [imgs, "Answer saved as PDF"])
        finish(s, d); page.evaluate("window.__lbOff()")

        # 9 bank file --------------------------------------------------------
        s, d = say("bank")
        click("#docs input[type=checkbox]"); time.sleep(0.5)
        click("#bankBtn")
        page.wait_for_function("document.querySelectorAll('a.chip').length>=2", timeout=240000)
        href = page.locator("a.chip").last.get_attribute("href")
        imgs, n = fetch_pdf_pages(href, 2)
        page.evaluate("([i,l]) => window.__lb(i,l)", [imgs, f"Bank file: {n} pages, Udyam certificate attached"])
        finish(s, d); page.evaluate("window.__lbOff()")

        # 10 loan application ------------------------------------------------
        s, d = say("app")
        click("#appBtn"); time.sleep(0.6)
        page.select_option("#aBank", "State Bank of India"); page.select_option("#aScheme", "MUDRA Kishore")
        click("#aAmt"); page.keyboard.type("300000", delay=60)
        click("#aPur"); page.keyboard.type("Buy two industrial sewing machines and cloth stock", delay=28)
        time.sleep(0.4)
        click("#aGo")
        page.wait_for_function("document.querySelectorAll('a.chip').length>=3", timeout=240000)
        href = page.locator("a.chip").last.get_attribute("href")
        imgs, n = fetch_pdf_pages(href, 2)
        page.evaluate("([i,l]) => window.__lb(i,l)", [imgs, f"Loan application draft: {n} pages, blanks marked, nothing submitted"])
        finish(s, d); page.evaluate("window.__lbOff()")

        # 11 glass box -------------------------------------------------------
        s, d = say("trace")
        click("details summary"); time.sleep(0.6)
        page.locator("#trace").scroll_into_view_if_needed(); time.sleep(0.6)
        page.evaluate("document.getElementById('side').scrollTo({top: 99999, behavior: 'smooth'})"); time.sleep(1.0)
        finish(s, d)

        # 12 outro -----------------------------------------------------------
        s, d = say("outro")
        page.evaluate("window.__card('SaralcreditAI', 'Compliance is how a small business becomes bankable.', 'github.com/sai20065/MunimAI')")
        finish(s, d, 1.2)
        total = now()

        video_path = page.video.path()
        ctx.close(); browser.close()

    print("take length", round(total, 1), "s", video_path)
    # soundtrack
    cmd = [FF, "-y", "-v", "error"]
    for _, pth in mix:
        cmd += ["-i", str(pth)]
    parts = [f"[{i}:a]adelay={int(t*1000)}|{int(t*1000)},volume={0.85 if 'n_' in str(pth) else 1.0}[a{i}]" for i, (t, pth) in enumerate(mix)]
    labels = "".join(f"[a{i}]" for i in range(len(mix)))
    cmd += ["-filter_complex", ";".join(parts) + f";{labels}amix=inputs={len(mix)}:normalize=0,apad[out]", "-map", "[out]",
            "-t", str(total), str(OUT / "mix.wav")]
    subprocess.run(cmd, check=True)
    final = OUT / "SaralcreditAI-launch-video.mp4"
    run("-i", str(video_path), "-i", str(OUT / "mix.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-t", str(total), str(final))
    print("DONE", final)


if __name__ == "__main__":
    main()
