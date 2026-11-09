"""Lagar app-ikonet til adminappen (mal-admin.html): same flamme som appen, med eit gult band «ADMIN» nedst
så ikona er lette å skilje på heimeskjermen. Køyr éin gong (treng Pillow og skrifta Segoe UI Bold på Windows)."""
from PIL import Image, ImageDraw, ImageFont

from lag_ikon import INDRE, UT, YTRE, punkt

SKRIFT = r"C:\Windows\Fonts\segoeuib.ttf"
BAND = 0.70       # bandet startar her (del av høgda)
GUL, MORK = (242, 194, 0, 255), (21, 24, 29, 255)


def ikon_admin(storleik):
    ss = 4  # supersampling for mjuke kantar
    s = storleik * ss
    bilete = Image.new("RGBA", (s, s), MORK)

    # Flamma: som i lag_ikon.py, men flytta opp så ho står midt i feltet over bandet
    skala = s / 34 * 0.80
    dx, dy = s / 2 - 17 * skala, s * 0.355 - 19.55 * skala      # 17 / 19.55 = midten av flamma i teikninga
    tilpass = lambda pts: [(dx + px * skala, dy + py * skala) for px, py in pts]
    maske = Image.new("L", (s, s), 0)
    ImageDraw.Draw(maske).polygon(tilpass(punkt(YTRE)), fill=255)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    topp, botn = dy + 11.1 * skala, dy + 28 * skala
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
    teikn = ImageDraw.Draw(bilete)
    teikn.polygon(tilpass(punkt(INDRE)), fill=(255, 233, 166, 235))

    # Bandet med «ADMIN»: teksten held seg innanfor den trygge sona når Android skjer ikonet til ein sirkel
    teikn.rectangle([0, int(s * BAND), s, s], fill=GUL)
    skrift = ImageFont.truetype(SKRIFT, int(s * 0.135))
    v, o, h, n = teikn.textbbox((0, 0), "ADMIN", font=skrift)
    teikn.text(((s - (h - v)) / 2 - v, s * 0.815 - (o + n) / 2), "ADMIN", font=skrift, fill=MORK)
    return bilete.resize((storleik, storleik), Image.LANCZOS)


if __name__ == "__main__":
    ikon_admin(512).save(UT / "admin-512.png")
    ikon_admin(192).save(UT / "admin-192.png")
    ikon_admin(180).save(UT / "admin-touch.png")
    print("Admin-ikon laga i", UT)
