"""
SaralcreditAI video 2: readiness, scheme fit, and the self-verifying bank file.

Same method as make_demo.py: narration is synthesised first, a scripted browser
take drives the real running app, ffmpeg mixes and encodes. Everything on screen
is real: the readiness score is computed from the seeded ledger, the scheme
verdicts come from the clause-bound rules, the seal and the tamper check run
against the live database. The only staging is that four months of bills are
inserted straight into the ledger (video 1 shows bills being read live).
"""
import asyncio
import base64
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
sys.path.insert(0, str(HERE.parent / "munimai"))
import psycopg  # noqa: E402
import backend.config  # noqa: E402,F401  (loads .env)
from backend.config import DBSettings  # noqa: E402
from make_demo import INIT as _INIT, FF, run, duration  # noqa: E402  (same overlays and ffmpeg helpers)
INIT = _INIT.replace('#__card h1{font-size:76px', '#__card h1{color:#fff!important;font-size:76px')

OUT = HERE / "out"; OUT.mkdir(exist_ok=True)
ASSETS = HERE / "assets"; ASSETS.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8088"
NARR_VOICE = "en-IN-NeerjaNeural"
SPEED = 6

NARR = {
 "intro": "SaralcreditAI speaks English, Hindi and Kannada. Now three new ideas that make it something a bank can trust: a readiness score, scheme fit with reasons, and a bank file that proves itself.",
 "setup": "Meet Anita, who runs a tailoring shop. Four months of her bills are already in her books.",
 "ready1": "First, she asks whether her books are ready for a lender. The assistant will not look until she says yes. Consent comes first, and she can switch it off at any time.",
 "ready2": "Now it scores her books and her GST discipline. Every point has a reason, and every missing point names the fix and what it is worth. The same bills that make her GST draft also decide how ready she looks to a bank.",
 "ready3": "She uploads her Udyam certificate. The number is read from it, and her score rises by ten points. And notice what it never claims: it cannot see whether returns were filed on time. Only the GST portal can.",
 "ready_kn": "Now in Kannada. She asks out loud how ready her books are, and the answer comes back in Kannada, spoken aloud.",
 "fit1": "Next, scheme fit. Anita answers a few questions, and leaves blank what she does not know.",
 "fit2": "Each scheme is checked against its published conditions, and every line shows the official clause it comes from. Startup India says no, because a proprietorship cannot be recognised, and it says exactly what would change that. No model wrote these verdicts. Each rule is code tied to a clause, and the verifier blocks any verdict that cannot cite a current one.",
 "fit3": "And in Hindi. She types the question in Hindi, and the answer comes back in Hindi, spoken aloud. Same checks, same clauses, any of the three languages.",
 "bank1": "Now the bank file. When she builds it, her books are sealed. Every ledger entry and every proof document goes into a hash chain, and a QR code is stamped on page one.",
 "verify1": "A bank officer scans the code and lands on a verify page. It shows the seal, the totals the PDF must match, and whether the books have changed since.",
 "verify2": "Suppose an entry is edited after sealing. The page does not hide it. The file still matches what was sealed, and the live books are flagged as different.",
 "verify3": "And if someone edits the PDF itself, the officer uploads it here, and it fails. The file must be byte for byte what was issued. The original passes.",
 "outro": "Readiness, fit with reasons, and a file that proves itself. SaralcreditAI. Compliance is how a small business becomes bankable.",
}
CAPS = {
 "intro": "Three new ideas: readiness, scheme fit, a self-verifying bank file.",
 "setup": "Anita, a tailor. Four months of bills in her books.",
 "ready1": "Consent first. Switch it off any time.",
 "ready2": "One ledger feeds both GST and credit readiness. Every point has a reason.",
 "ready3": "Upload the Udyam certificate: +10 points. It never claims filing status.",
 "ready_kn": "Kannada voice in, Kannada voice out.",
 "fit1": "Scheme fit: answer what you know.",
 "fit2": "Every verdict cites the official clause. No clause, no claim.",
 "fit3": "Hindi in, Hindi out. Same clauses.",
 "bank1": "The books are sealed into the bank file. QR on page one.",
 "verify1": "The bank scans the QR and sees the seal and the totals.",
 "verify2": "Edit the books after sealing and the page shows it.",
 "verify3": "An edited PDF fails. The original passes.",
 "outro": "Compliance is how a small business becomes bankable.",
}


def build_assets():
    async def tts(text, out):
        await edge_tts.Communicate(text, NARR_VOICE, rate="-4%").save(str(out))
    nar = {}
    for k, text in NARR.items():
        p = ASSETS / f"v2_{k}.mp3"
        if not p.exists():
            asyncio.run(tts(text, p))
        nar[k] = (p, duration(p))
    q2 = ASSETS / "q2.mp3"
    if not q2.exists():
        asyncio.run(edge_tts.Communicate("ಸಾಲಕ್ಕಾಗಿ ನನ್ನ ಪುಸ್ತಕಗಳು ಎಷ್ಟು ಸಿದ್ಧವಾಗಿವೆ?", "kn-IN-SapnaNeural").save(str(q2)))
    run("-i", str(q2), "-af", "adelay=700|700,apad=pad_dur=4", "-ar", "16000", "-ac", "1", str(ASSETS / "mic2.wav"))
    udyam = ASSETS / "udyam.pdf"
    if not udyam.exists():
        d = pymupdf.open(); pg = d.new_page()
        pg.insert_text((50, 70), "\n".join(["UDYAM REGISTRATION CERTIFICATE", "Udyam Registration Number: UDYAM-KA-18-0012345",
                       "Name of Enterprise: Anita Tailors", "Type of Enterprise: Micro", "Major Activity: Manufacturing",
                       "Date of Incorporation: 04/02/2021", "Official address: 12 Market Road, Mysuru, Karnataka"]), fontsize=11)
        d.save(str(udyam))
    return nar


def seed_books(munim_id):
    c = psycopg.connect(DBSettings.from_env().conninfo)
    bid = c.execute("SELECT id FROM businesses WHERE whatsapp_phone=%s", ("web:" + munim_id,)).fetchone()[0]
    sales = [(6, 52000), (7, 47000), (8, 61000), (9, 58000)]
    costs = [(6, 21000), (7, 19000), (8, 24000), (9, 22000)]
    for m, v in sales:
        c.execute("INSERT INTO ledger_entries (business_id, entry_date, direction, amount_paise, category, counterparty, gst_rate_bp, source)"
                  " VALUES (%s,%s,'in',%s,'stitching','Walk-in customers',500,'manual')", (bid, f"2026-{m:02d}-08", v * 100))
    for m, v in costs:
        c.execute("INSERT INTO ledger_entries (business_id, entry_date, direction, amount_paise, category, counterparty, gst_rate_bp, source)"
                  " VALUES (%s,%s,'out',%s,'cloth','Mysore Silk Traders',500,'manual')", (bid, f"2026-{m:02d}-15", v * 100))
    c.execute("INSERT INTO ledger_entries (business_id, entry_date, direction, amount_paise, category, counterparty, gst_rate_bp, source, needs_review)"
              " VALUES (%s,'2026-09-20','out',350000,'thread and buttons',NULL,NULL,'manual',TRUE)", (bid,))
    c.commit()
    return c, bid


def main():
    nar = build_assets()
    print({k: round(v[1], 1) for k, v in nar.items()})
    mix = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={ASSETS / 'mic2.wav'}%noloop", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(OUT),
                                  record_video_size={"width": 1280, "height": 720}, permissions=["microphone"])
        ctx.add_init_script("try{localStorage.setItem('munim_lang','en')}catch(e){}")
        ctx.add_init_script(INIT)
        page = ctx.new_page()
        t0 = time.time()
        now = lambda: time.time() - t0

        def say(key):
            print('scene', key, round(now(), 1), flush=True)
            mix.append((now(), nar[key][0]))
            page.evaluate("t => window.__cap(t)", CAPS[key])
            return now(), nar[key][1]

        def finish(start, dur, extra=0.7):
            left = start + dur + extra - now()
            if left > 0:
                time.sleep(left)
            page.evaluate("window.__cap('')")

        def move_to(sel, steps=26):
            page.locator(sel).first.scroll_into_view_if_needed(); time.sleep(0.25)
            box = page.locator(sel).first.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
            time.sleep(0.2)

        def click(sel):
            move_to(sel); page.mouse.down(); time.sleep(0.08); page.mouse.up(); time.sleep(0.3)

        def ask(text):
            n = page.locator(".msg.them").count()
            click("#msg"); page.keyboard.type(text, delay=34); time.sleep(0.3)
            page.keyboard.press("Enter")
            page.wait_for_function(f"document.querySelectorAll('.msg.them').length>{n}", timeout=30000)
            page.wait_for_function("document.getElementById('orb').dataset.s==='idle'", timeout=150000)
            time.sleep(0.6)

        fast = []   # (start, end) of waits to speed up; narration is never playing during these

        def fast_wait(fn, label="Working...  (sped up)"):
            page.evaluate("t => window.__cap(t)", label)
            a = now(); fn(); b = now(); page.evaluate("window.__cap('')")
            print('wait', label, round(b - a, 1), flush=True)
            if b - a > 4:
                fast.append((a + 1.0, b))

        seen = [0]

        def collect_clips():
            clips = page.evaluate("window.__clips")
            for c in clips[seen[0]:]:
                wav = ASSETS / f"v2clip_{len(mix)}.wav"
                wav.write_bytes(base64.b64decode(c["src"].split(",", 1)[1]))
                mix.append((nav_t + c["t"] / 1000.0, wav))
            seen[0] = len(clips)

        def set_lang(code):
            click(f".seg button[data-l={code}]"); time.sleep(0.4)

        def pdf_pages(data, n=2, dpi=100):
            d = pymupdf.open(stream=data, filetype="pdf")
            return ["data:image/png;base64," + base64.b64encode(d[i].get_pixmap(dpi=dpi).tobytes("png")).decode()
                    for i in range(min(n, len(d)))], len(d)

        page.goto(BASE + "/sim/")
        page.wait_for_selector("#setup.on", timeout=20000)
        nav_t = now()

        # 1 intro
        s, d = say("intro")
        page.evaluate("window.__card('SaralcreditAI', 'Readiness. Scheme fit. A bank file that proves itself.', 'Three new ideas')")
        finish(s, d, 0.4); page.evaluate("window.__cardOff()")

        # 2 profile + seeded books
        s, d = say("setup")
        click("#bName"); page.keyboard.type("Anita Tailors", delay=60)
        click("#bGst"); page.keyboard.type("29ABCDE1234F1Z5", delay=50); time.sleep(0.3)
        click("#setupSave"); time.sleep(0.8)
        munim_id = page.evaluate("localStorage.getItem('munim_id')")
        conn, bid = seed_books(munim_id)
        page.evaluate("loadBooks()"); time.sleep(0.8)
        finish(s, d)

        # 3 readiness: consent first
        s, d = say("ready1")
        time.sleep(0.8)
        ask("How ready are my books for a loan?")
        page.wait_for_function("document.getElementById('consentRow').classList.contains('flash')", timeout=10000)
        time.sleep(1.2)
        click("#consent"); time.sleep(1.5)
        finish(s, d)

        s, d = say("ready_kn")
        set_lang("kn"); time.sleep(0.8)
        click("#orb")
        page.wait_for_function("document.getElementById('orb').dataset.s==='listening'", timeout=15000)
        page.wait_for_function("document.getElementById('orb').dataset.s==='thinking'", timeout=40000)
        page.wait_for_selector(".msg.them .bubble:not(:has(.dots))", timeout=120000)
        finish(s, d, 0.2)
        page.wait_for_function("document.getElementById('orb').dataset.s==='speaking'", timeout=90000)
        try:
            page.wait_for_function("document.getElementById('orb').dataset.s==='idle'", timeout=70000)
        except Exception:
            click("#orb")
        collect_clips(); time.sleep(0.8)

        s, d = say("ready2")
        set_lang("en")
        page.locator("#ready").scroll_into_view_if_needed(); time.sleep(0.5)
        page.evaluate("document.getElementById('side').scrollBy({top: 140, behavior: 'smooth'})")
        finish(s, d)

        # 4 upload Udyam: score rises
        s, d = say("ready3")
        page.locator("#readySec").scroll_into_view_if_needed()
        before = page.locator("#ready .score b").first.inner_text()
        move_to("#attach"); time.sleep(0.3)
        page.set_input_files("#file", str(ASSETS / "udyam.pdf"))
        finish(s, d)
        fast_wait(lambda: page.wait_for_function(
            "b => document.querySelector('#ready .score b') && document.querySelector('#ready .score b').textContent !== b",
            arg=before, timeout=150000), "Reading the certificate...  (sped up)")
        page.locator("#readySec").scroll_into_view_if_needed(); time.sleep(2.2)
        print("readiness", before, "->", page.locator("#ready .score b").first.inner_text())

        # 5 scheme fit form
        s, d = say("fit1")
        click("#fitBtn"); page.wait_for_selector("#f_activity", timeout=15000); time.sleep(0.5)
        vals = [("f_activity", "manufacturing"), ("f_gender", "female"), ("f_category", "general"), ("f_education", "10th"),
                ("f_new_business", "true"), ("f_availed_govt_subsidy", "false"), ("f_loan_last_5y", "false"),
                ("f_street_vendor", "false"), ("f_artisan_trade", "true"), ("f_govt_service", "false"),
                ("f_entity_type", "proprietorship"), ("f_innovative", "false"), ("f_msme_category", "micro")]
        for i, (fid, v) in enumerate(vals):
            if i < 3:
                move_to("#" + fid)
            page.select_option("#" + fid, v); time.sleep(0.25 if i < 3 else 0.05)
        click("#f_loan_amount"); page.keyboard.type("1500000", delay=60)
        click("#f_age"); page.keyboard.type("34", delay=60)
        page.fill("#f_years_incorporated", "5"); time.sleep(0.3)
        finish(s, d)

        s, d = say("fit2")
        click("#fitGo"); page.wait_for_selector("#fitOut details.sc", timeout=60000); time.sleep(1.0)
        page.locator("#fitOut details.sc", has_text="PMEGP").first.scroll_into_view_if_needed(); time.sleep(2.5)
        page.locator("#fitOut details.sc", has_text="Startup India").first.scroll_into_view_if_needed(); time.sleep(3.0)
        finish(s, d)
        click("#fitClose"); time.sleep(0.5)

        # 6 chat
        s, d = say("fit3")
        set_lang("hi"); time.sleep(0.4)
        n0 = page.locator(".msg.them").count()
        click("#msg"); page.keyboard.type("स्टार्टअप योजना के लिए मैं पात्र कैसे बन सकती हूँ?", delay=45); time.sleep(0.3)
        page.keyboard.press("Enter")
        page.wait_for_function(f"document.querySelectorAll('.msg.them').length>{n0}", timeout=60000)
        time.sleep(1.0)
        try:
            page.wait_for_function("document.getElementById('orb').dataset.s==='speaking'", timeout=60000)
            page.wait_for_function("document.getElementById('orb').dataset.s==='idle'", timeout=80000)
        except Exception:
            pass
        collect_clips()
        finish(s, d)
        set_lang("en")

        # 7 sealed bank file
        s, d = say("bank1")
        click("#bankBtn")
        finish(s, d)
        fast_wait(lambda: page.wait_for_function("document.querySelectorAll('a.chip').length>=2", timeout=240000),
                  "Sealing the books and building the file...  (sped up)")
        s, d = now(), 3.0
        href = page.locator("a.chip", has_text="SaralcreditAI-bank-file").last.get_attribute("href")
        verify = page.locator("a.chip", has_text="Verify page").last.get_attribute("href")
        good_pdf = page.request.get(BASE + href).body()
        (ASSETS / "bank_good.pdf").write_bytes(good_pdf)
        (ASSETS / "bank_edited.pdf").write_bytes(good_pdf + b"\n% edited after issue\n")
        imgs, n = pdf_pages(good_pdf, 1, 110)
        page.evaluate("([i,l]) => window.__lb(i,l)", [imgs, "Page 1: books sealed, scan to verify"])
        time.sleep(3.0); page.evaluate("window.__lbOff()")

        # 8 verify page
        s, d = say("verify1")
        page.goto(BASE + verify); time.sleep(1.0)
        finish(s, d)

        s, d = say("verify2")
        conn.execute("UPDATE ledger_entries SET amount_paise=amount_paise+250000 WHERE business_id=%s AND direction='in' AND entry_date='2026-07-08'", (bid,))
        conn.commit()
        time.sleep(1.2); page.reload(); time.sleep(1.0)
        finish(s, d)

        s, d = say("verify3")
        page.set_input_files("input[type=file]", str(ASSETS / "bank_edited.pdf")); time.sleep(0.6)
        click("button[type=submit]"); page.wait_for_selector("text=does NOT match", timeout=20000)
        page.locator("text=does NOT match").scroll_into_view_if_needed(); time.sleep(2.5)
        page.set_input_files("input[type=file]", str(ASSETS / "bank_good.pdf")); time.sleep(0.6)
        click("button[type=submit]"); page.wait_for_selector("text=exactly the file", timeout=20000)
        page.locator("text=exactly the file").scroll_into_view_if_needed(); time.sleep(1.5)
        finish(s, d)

        # 9 outro
        s, d = say("outro")
        page.evaluate("window.__card('SaralcreditAI', 'Compliance is how a small business becomes bankable.', 'English · हिन्दी · ಕನ್ನಡ   ·   github.com/sai20065/MunimAI')")
        finish(s, d, 1.2)
        total = now()
        video_path = page.video.path()
        ctx.close(); browser.close()

    print("take length", round(total, 1), "s", video_path)
    def remap(t):
        off = 0.0
        for a, b in fast:
            if t >= b:
                off += (b - a) * (1 - 1 / SPEED)
            elif t > a:
                off += (t - a) * (1 - 1 / SPEED)
        return t - off
    mix[:] = [(remap(t), pth) for t, pth in mix]
    new_total = remap(total)
    if fast:
        segs, last = [], 0.0
        for a, b in sorted(fast):
            segs += [(last, a, 1), (a, b, SPEED)]
            last = b
        segs.append((last, total, 1))
        fc = [f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{f}[v{i}]" for i, (a, b, f) in enumerate(segs)]
        fc.append("".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[vout]")
        sped = OUT / "sped.mp4"
        run("-i", str(video_path), "-filter_complex", ";".join(fc), "-map", "[vout]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "20", "-r", "30", str(sped))
        video_path = sped
        total = new_total
    cmd = [FF, "-y", "-v", "error"]
    for _, pth in mix:
        cmd += ["-i", str(pth)]
    parts = [f"[{i}:a]adelay={int(t*1000)}|{int(t*1000)},volume=0.9[a{i}]" for i, (t, pth) in enumerate(mix)]
    labels = "".join(f"[a{i}]" for i in range(len(mix)))
    cmd += ["-filter_complex", ";".join(parts) + f";{labels}amix=inputs={len(mix)}:normalize=0,apad[out]", "-map", "[out]",
            "-t", str(total), str(OUT / "mix2.wav")]
    subprocess.run(cmd, check=True)
    final = OUT / "SaralcreditAI-v2-trust-video.mp4"
    run("-i", str(video_path), "-i", str(OUT / "mix2.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-t", str(total), str(final))
    print("DONE", final)


if __name__ == "__main__":
    main()
