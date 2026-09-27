"""Lagar app-ikon (flamme-logoen) til nettside-versjonen. Køyr éin gong."""
import re
from pathlib import Path

from PIL import Image, ImageDraw

UT = Path(__file__).parent / "nettside"
YTRE = "M17 28c-4.6 0-7.6-3-7.6-7 0-4.1 3-6.4 4.2-9.9 1.6 1.6 2.3 3.4 2.3 5.1 1.2-1.2 2.1-3 2.3-5.1 3.5 2.6 6.4 6.4 6.4 9.9 0 4-3 7-7.6 7Z"
INDRE = "M17 28c-2 0-3.3-1.3-3.3-3.1 0-1.8 1.4-2.9 2-4.4 1.6 1.2 4.6 2.6 4.6 4.4 0 1.8-1.3 3.1-3.3 3.1Z"


def punkt(sti, steg=24):
    """Omset ein enkel SVG-sti (M + relative c-kurver) til ei punktliste."""
    tal = [float(t) for t in re.findall(r"-?\d*\.?\d+", sti)]
    x, y = tal[0], tal[1]
    ut = [(x, y)]
    rest = tal[2:]
    for i in range(0, len(rest), 6):
        x1, y1, x2, y2, x3, y3 = rest[i:i + 6]
        p0, p1, p2, p3 = (x, y), (x + x1, y + y1), (x + x2, y + y2), (x + x3, y + y3)
        for s in range(1, steg + 1):
            t = s / steg
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
            ut.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
        x, y = p3
    return ut


def ikon(storleik, avrunda):
    ss = 4  # supersampling for mjuke kantar
    s = storleik * ss
    bilete = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    teikn = ImageDraw.Draw(bilete)
    bg = (21, 24, 29, 255)
    if avrunda:
        teikn.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=bg)
    else:
        teikn.rectangle([0, 0, s, s], fill=bg)

    skala, forskyv = s / 34 * 0.86, s * 0.07
    tilpass = lambda pts: [(forskyv + px * skala, forskyv * 0.7 + py * skala) for px, py in pts]

    # Gradient-flamme: raud nedst, gul øvst
    maske = Image.new("L", (s, s), 0)
    ImageDraw.Draw(maske).polygon(tilpass(punkt(YTRE)), fill=255)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    topp, botn = int(s * 0.25), int(s * 0.9)
    stopp = [(0.0, (212, 50, 29)), (0.6, (240, 138, 18)), (1.0, (247, 203, 46))]
    for yy in range(s):
        t = min(1, max(0, (botn - yy) / (botn - topp)))
        for (t0, c0), (t1, c1) in zip(stopp, stopp[1:]):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                farge = tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)) + (255,)
                break
        gd.line([(0, yy), (s, yy)], fill=farge)
    bilete.paste(grad, (0, 0), maske)
    ImageDraw.Draw(bilete).polygon(tilpass(punkt(INDRE)), fill=(255, 233, 166, 235))
    return bilete.resize((storleik, storleik), Image.LANCZOS)


if __name__ == "__main__":
    UT.mkdir(exist_ok=True)
    ikon(512, False).save(UT / "ikon-512.png")
    ikon(192, False).save(UT / "ikon-192.png")
    ikon(180, False).save(UT / "apple-touch-icon.png")
    ikon(32, True).save(UT / "favicon.png")
    print("Ikon laga i", UT)
