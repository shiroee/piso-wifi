"""Two A4 pages, three steps each, in the neon gaming style."""
import json
from pathlib import Path

from playwright.async_api import async_playwright

import config
from screens import price

HERE = Path(__file__).resolve().parent / "build"
OUT = Path(__file__).resolve().parents[2] / "docs" / "images" / "print"
NAME, SSID = config.NAME, config.SSID


def time_label(m):
    d, rest = divmod(m, 1440)
    h, mins = divmod(rest, 60)
    if d:
        return f"{d} day" + ("s" if d != 1 else "") + (f" {h} hr" if h else "")
    if h:
        return f"{h} hr" + ("s" if h != 1 else "") + (f" {mins} min" if mins else "")
    return f"{mins} min"


RATES = [(f"₱{a}", time_label(price(a))) for a in sorted({1, 5, 10, 20} | {p for p, _ in config.PROMOS})]
BEST = f"₱{config.BEST_VALUE}" if config.BEST_VALUE else None
BOXES = json.loads((HERE / "boxes.json").read_text())
INNER = 176
SCALE = INNER / 360
BEZEL = 8

def tap(shot, selector, fx=0.5, fy=0.5):
    x, y, w, h = BOXES[shot][selector]
    cx, cy = BEZEL + (x + w * fx) * SCALE, BEZEL + (y + h * fy) * SCALE
    return f'<div class="tap" style="left:{cx:.0f}px;top:{cy:.0f}px"><i></i><i></i><b></b><span>TAP</span></div>'

SLOT = '''<svg class="slot" viewBox="0 0 120 92" aria-hidden="true">
  <defs><radialGradient id="g" cx="34%" cy="28%" r="80%"><stop offset="0" stop-color="#FFE29A"/><stop offset=".4" stop-color="#F5B83D"/><stop offset="1" stop-color="#C98408"/></radialGradient>
  <filter id="b" x="-50%" y="-300%" width="200%" height="700%"><feGaussianBlur stdDeviation="4"/></filter></defs>
  <rect x="10" y="44" width="100" height="44" rx="6" fill="#120F2A" stroke="#19F0FF" stroke-width="2"/>
  <rect x="26" y="60" width="68" height="10" rx="5" fill="#19F0FF" filter="url(#b)"/>
  <rect x="26" y="60" width="68" height="10" rx="5" fill="#B8FBFF"/>
  <rect x="31" y="63" width="58" height="4" rx="2" fill="#07060F"/>
  <path d="M60 30v10M54 35l6 6 6-6" fill="none" stroke="#FF2BD6" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="60" cy="15" r="13" fill="url(#g)"/>
  <text x="60" y="21" text-anchor="middle" font-family="DejaVu Sans" font-weight="800" font-size="16" fill="#3A2600">₱</text>
</svg>'''

PAGES = [
    dict(title=f"Connect to {NAME}", kicker="QUEST 1 · KUMONEKTA", steps=[
        dict(n=1, shot="wifi", tap=("#piso", 0.78, 0.5), title=f"Join “{SSID}”", tl="Kumonekta sa Wi-Fi",
             notes=[("Choose <b>" + SSID + "</b> in your Wi-Fi settings. No password.", "Piliin ito sa Wi-Fi settings. Walang password."),
                    ("“No internet” is normal until you insert coins.", "Normal ang “No internet” hangga't wala pang barya.")]),
        dict(n=2, shot="start", tap=None, title="The Wi-Fi page loads", tl="Lalabas ang Wi-Fi page",
             notes=[("It opens by itself, or tap “Sign in to network”.", "Kusa itong lalabas, o i-tap ang “Sign in”."),
                    ("Not showing? Go to <span class=\"url\">http://piso.wifi</span>", "Kung wala, buksan ang http://piso.wifi")]),
        dict(n=3, shot="start", tap=("#b-insert", 0.7, 0.72), title="Tap the gold coin", tl="I-tap ang gintong barya",
             notes=[("Tap it <b>before</b> inserting coins. The coin slot lights up.", "I-tap muna bago maghulog. Iilaw ang coin slot."),
                    ("Coins dropped before tapping won't count.", "Hindi bibilang ang baryang naihulog bago mag-tap.")]),
    ]),
    dict(title="Insert coins. Game on.", kicker="QUEST 2 · MAGHULOG AT MAG-INTERNET", steps=[
        dict(n=4, shot="window", tap=None, slot=True, title="Drop your coins", tl="Ihulog ang barya sa slot",
             notes=[(f"<b>{config.WINDOW_SECONDS} seconds</b> on the clock. Every coin adds time and resets the timer.", f"{config.WINDOW_SECONDS} segundo. Bawat barya ay dagdag oras."),
                    ("The slot light blinks for every coin.", "Kumikislap ang ilaw sa bawat barya.")]),
        dict(n=5, shot="insert", tap=("#b-start", 0.62, 0.5), title="Tap “Start my time”", tl="I-tap ang “Simulan ang oras ko”",
             notes=[("Your coins and time are shown above the button.", "Makikita ang barya at oras mo."),
                    ("Forgot to tap? Your time starts when the timer hits zero.", "Kusang magsisimula pag naubos ang timer.")]),
        dict(n=6, shot="online", tap=None, title="You're online. GG!", tl="Online ka na!",
             notes=[("<b>Add time</b> to buy more. <b>Pause</b> to save time for your next session.", "Add time para dagdag. Pause para i-save ang oras."),
                    ("Check your time at <span class=\"url\">http://piso.wifi</span>", "Tingnan ang oras sa http://piso.wifi")]),
    ]),
]


CSS = '''
:root { --bg:#07060F; --panel:#100E22; --panel-2:#17143A; --text:#F1F0FF; --muted:#A9A6D6;
  --cyan:#19F0FF; --magenta:#FF2BD6; --lime:#A6FF3D; --gold:#F5B83D; --gold-hi:#FFE29A; --gold-deep:#C98408; --gold-ink:#3A2600; }
* { box-sizing:border-box; margin:0; padding:0; }
body { width:794px; height:1123px; color:var(--text); font-family:"DejaVu Sans", system-ui, sans-serif;
  background:
    radial-gradient(60% 40% at 0% 0%, rgba(255,43,214,.22), transparent 70%),
    radial-gradient(60% 45% at 100% 100%, rgba(25,240,255,.18), transparent 70%),
    repeating-linear-gradient(0deg, rgba(25,240,255,.05) 0 1px, transparent 1px 34px),
    repeating-linear-gradient(90deg, rgba(25,240,255,.05) 0 1px, transparent 1px 34px),
    var(--bg); }
.mono { font-family:"DejaVu Sans Mono", monospace; letter-spacing:.14em; }
.sheet { height:100%; display:flex; flex-direction:column; padding:26px 38px 24px; }
header { display:flex; align-items:center; gap:16px; }
.coin { border-radius:50%; display:grid; place-items:center; color:var(--gold-ink); font-weight:850; flex:none;
  background:radial-gradient(circle at 34% 28%, var(--gold-hi) 0, var(--gold) 38%, var(--gold-deep) 88%);
  box-shadow: inset 0 0 0 4px rgba(255,255,255,.25), inset 0 -5px 10px rgba(0,0,0,.25), 0 0 22px rgba(245,184,61,.45); }
header .coin { width:62px; height:62px; font-size:32px; }
.kicker { font-size:12.5px; color:var(--cyan); font-weight:700; text-shadow:0 0 10px rgba(25,240,255,.7); }
header h1 { font-size:26px; font-style:italic; font-weight:900; text-transform:uppercase; letter-spacing:.01em; line-height:1.05; margin-top:4px;
  text-shadow: 2px 2px 0 var(--magenta), 0 0 18px rgba(255,43,214,.45); }
.badge { margin-left:auto; font-size:12.5px; font-weight:700; padding:7px 14px; color:var(--bg); background:var(--cyan);
  clip-path:polygon(10px 0,100% 0,calc(100% - 10px) 100%,0 100%); box-shadow:0 0 18px rgba(25,240,255,.6); white-space:nowrap; }
.alert { margin-top:18px; padding:2px; background:linear-gradient(90deg,var(--magenta),var(--cyan));
  clip-path:polygon(12px 0,100% 0,100% calc(100% - 12px),calc(100% - 12px) 100%,0 100%,0 12px); }
.alert div { background:#1A0B24; padding:10px 16px; font-size:14px; color:#FFD0F4;
  clip-path:polygon(11px 0,100% 0,100% calc(100% - 11px),calc(100% - 11px) 100%,0 100%,0 11px); }
.alert b { color:#fff; font-size:15px; }
.alert .mono { color:var(--magenta); font-weight:700; margin-right:8px; font-size:12.5px; }
.steps { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:18px; flex:1; }
.frame { padding:2px; background:linear-gradient(160deg, var(--cyan), rgba(25,240,255,.15) 40%, rgba(255,43,214,.2) 65%, var(--magenta));
  clip-path:polygon(16px 0,100% 0,100% calc(100% - 16px),calc(100% - 16px) 100%,0 100%,0 16px); display:flex; min-width:0; }
.step { flex:1; display:flex; flex-direction:column; min-width:0; background:linear-gradient(180deg,var(--panel-2),var(--panel)); padding:14px 13px 12px;
  clip-path:polygon(15px 0,100% 0,100% calc(100% - 15px),calc(100% - 15px) 100%,0 100%,0 15px); }
.level { display:flex; align-items:center; justify-content:space-between; }
.level .mono { font-size:11.5px; color:var(--cyan); font-weight:700; }
.level .num { font-size:34px; font-weight:900; font-style:italic; color:transparent; -webkit-text-stroke:1.5px var(--cyan); line-height:1; text-shadow:0 0 14px rgba(25,240,255,.5); }
.step h2 { font-size:17px; font-weight:900; font-style:italic; text-transform:uppercase; line-height:1.15; margin-top:6px; min-height:40px; }
.step h3 { font-size:12.5px; color:var(--muted); font-weight:400; margin-top:3px; }
.phone { position:relative; align-self:center; width:192px; border-radius:28px; background:#05040B; padding:8px; margin:12px 0;
  box-shadow:0 0 0 1.5px rgba(25,240,255,.55), 0 0 26px rgba(25,240,255,.25), 0 18px 34px -16px rgba(0,0,0,.8); }
.phone img { display:block; width:176px; border-radius:21px; }
.tap { position:absolute; width:0; height:0; }
.tap i { position:absolute; left:-27px; top:-27px; width:54px; height:54px; border-radius:50%; border:3px solid var(--magenta); box-shadow:0 0 14px var(--magenta); }
.tap i + i { left:-39px; top:-39px; width:78px; height:78px; border-width:2px; border-color:rgba(255,43,214,.5); box-shadow:none; }
.tap b { position:absolute; left:-9px; top:-9px; width:18px; height:18px; border-radius:50%; background:#fff; box-shadow:0 0 0 4px rgba(255,43,214,.65), 0 0 16px #fff; }
.tap span { position:absolute; left:26px; top:-42px; background:var(--magenta); color:#fff; font:700 12px "DejaVu Sans Mono",monospace; letter-spacing:.14em;
  padding:3px 9px; clip-path:polygon(6px 0,100% 0,calc(100% - 6px) 100%,0 100%); }
.notes { display:grid; gap:9px; }
.notes p { font-size:13.5px; line-height:1.35; }
.notes p::before { content:"▸ "; color:var(--lime); }
.notes p span.tl { display:block; color:var(--muted); font-size:12px; margin-top:2px; padding-left:14px; }
.url { font-weight:800; color:var(--bg); background:var(--cyan); padding:0 4px; white-space:nowrap; }
.slot { display:block; width:94px; margin:0 auto 2px; filter:drop-shadow(0 0 10px rgba(25,240,255,.45)); }
.shop { margin-top:16px; padding:2px; background:linear-gradient(90deg,var(--gold),var(--magenta) 55%,var(--cyan));
  clip-path:polygon(14px 0,100% 0,100% calc(100% - 14px),calc(100% - 14px) 100%,0 100%,0 14px); }
.shop-in { background:#0C0A1D; padding:12px 14px;
  clip-path:polygon(13px 0,100% 0,100% calc(100% - 13px),calc(100% - 13px) 100%,0 100%,0 13px); }
.shop-head { display:flex; align-items:baseline; gap:10px; margin-bottom:9px; }
.shop-head b { font-size:17px; font-style:italic; font-weight:900; text-transform:uppercase; color:var(--gold); text-shadow:0 0 12px rgba(245,184,61,.5); }
.shop-head span { font-size:12px; color:var(--muted); }
.rates { display:grid; grid-template-columns:repeat(6,1fr); gap:6px; }
.rate { display:flex; align-items:center; gap:6px; background:var(--panel-2); padding:5px 6px; border:1px solid rgba(25,240,255,.18); }
.rate .coin { width:30px; height:30px; font-size:10.5px; box-shadow: inset 0 0 0 3px rgba(255,255,255,.25); }
.rate b { font-size:12.5px; white-space:nowrap; }
.rate.best { grid-column:span 2; border-color:var(--lime); box-shadow:0 0 12px rgba(166,255,61,.35) inset; }
.rate.best em { margin-left:auto; font:700 10px "DejaVu Sans Mono",monospace; letter-spacing:.1em; color:var(--bg); background:var(--lime); padding:2px 6px; font-style:normal; }
footer { display:flex; align-items:center; justify-content:space-between; margin-top:14px; color:var(--muted); font-size:13px; }
.xp { display:flex; align-items:center; gap:10px; }
.xp .mono { font-size:11px; color:var(--cyan); }
.bar { display:flex; gap:4px; }
.bar span { width:26px; height:9px; background:rgba(255,255,255,.12); transform:skewX(-20deg); }
.bar span.on { background:var(--lime); box-shadow:0 0 10px rgba(166,255,61,.6); }
'''

def step_html(s):
    shot = HERE / f"{s['shot']}.png"
    t = tap(s["shot"], *s["tap"]) if s.get("tap") else ""
    slot = SLOT if s.get("slot") else ""
    notes = "".join(f'<p>{en}<span class="tl">{tl}</span></p>' for en, tl in s["notes"])
    return f'''<div class="frame"><section class="step">
      <div class="level"><span class="mono">LEVEL {s["n"]}</span><span class="num">0{s["n"]}</span></div>
      <h2>{s["title"]}</h2><h3>{s["tl"]}</h3>
      <div class="phone"><img src="file://{shot}">{t}</div><div class="notes">{slot}{notes}</div></section></div>'''

def page_html(i, page):
    steps = "".join(step_html(s) for s in page["steps"])
    alert = ('<div class="alert"><div><span class="mono">WARNING</span><b>Tap the coin on your phone first, then insert your coins.</b> '
             'I-tap muna ang barya bago maghulog.</div></div>') if i == 1 else ""
    rates = "".join(
        f'<div class="rate{" best" if c == BEST else ""}"><div class="coin">{c}</div><b>{t}</b>{"<em>BEST VALUE</em>" if c == BEST else ""}</div>'
        for c, t in RATES)
    done = 3 if i == 1 else 6
    bar = "".join(f'<span class="{"on" if k < done else ""}"></span>' for k in range(6))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="sheet">
  <header><div class="coin">₱</div><div><p class="kicker mono">{page["kicker"]}</p><h1>{page["title"]}</h1></div><span class="badge mono">PAGE {i}/2</span></header>
  {alert}<div class="steps">{steps}</div>
  <div class="shop"><div class="shop-in"><div class="shop-head"><b>Coin shop</b><span>Rates · Presyo · Bigger coins, bigger bonus</span></div>
    <div class="rates">{rates}</div></div></div>
  <footer><div class="xp"><span class="mono">PROGRESS</span><div class="bar">{bar}</div></div>
    <span><b style="color:#fff">{NAME}</b> · Need help? Ask the owner. Magtanong sa may-ari.</span></footer>
</div></body></html>'''

async def main():
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 794, "height": 1123}, device_scale_factor=3)
        pg = await ctx.new_page()
        for i, page in enumerate(PAGES, start=1):
            html = HERE / f"steps{i}.html"
            html.write_text(page_html(i, page))
            await pg.goto(f"file://{html}")
            await pg.wait_for_timeout(300)
            print(i, "footer bottom", round(await pg.evaluate("document.querySelector('footer').getBoundingClientRect().bottom")))
            await pg.screenshot(path=str(out / f"steps-page-{i}.png"))
        await b.close()
