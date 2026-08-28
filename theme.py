"""Qt palette, generated from ojee-ui.css.

The desktop app and the web module are the same product, so they must not
drift. They already had: this app was on `#0e0d0a` — a WARM near-black — while
the design system had moved to `#08080e`, which its own comment describes as
"NOT warm". Nobody noticed because nothing checked.

So the palette is no longer typed in. It is parsed out of the vendored
ojee-ui.css at import, which means a token change in the design system reaches
this app the next time ojee-ui.css is synced, and a mismatch is impossible
rather than merely unlikely.

The fallback exists so a missing or malformed stylesheet degrades to the
correct-looking app rather than crashing on launch. It is the same values, and
it is checked against the CSS by tests/check-theme.py.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).with_name("ojee-ui.css")

# ojee-ui token -> the name this app has always used for it. Keeping the old
# names means the ~260 stylesheet strings scattered through the UI keep working
# untouched; only where the colours COME FROM changes.
TOKEN_MAP = {
    "BG":         "--bg",
    "CARD":       "--bg-2",
    "TEXT":       "--ink",
    "TEXT_DIM":   "--ink-2",
    "TEXT_MUTED": "--dim",
    "BTN_ACT":    "--accent",
    "BTN_ACT_T":  "--on-accent",
    "ACCENT":     "--accent",
    "CORE_LO":    "--cell-0",
    "CORE_MED":   "--cell-1",
    "CORE_HI":    "--cell-2",
    "CORE_MAX":   "--cell-3",
    # Qt has no alpha compositing in these stylesheet strings, so the
    # translucent tokens are resolved to their composited value below.
    # The threshold colours. The desktop had none, so every metric tile
    # carried a hand-picked hue instead — a rainbow the web build does not
    # have. With these it can say "warn" and "err" the same way.
    "WARN":       "--warn",
    "ERR":        "--err",
    "LINE":       "--line-2",
    "BORDER":     "--line-strong",
    "GR_GRID":    "--line-strong",
    "GR_TEXT":    "--dim",
}

FALLBACK = dict(
    BG="#08080e", CARD="#0e0e15", BORDER="#606370",
    TEXT="#e8e6df", TEXT_DIM="#a8a59c", TEXT_MUTED="#8a8478",
    BTN_DEF="#0d0d13", BTN_HOVER="#0b1417",
    BTN_ACT="#00ffff", BTN_ACT_T="#000000", BTN_ACT_H="#7fffff",
    TOG_ON="#00ffff", TOG_OFF="#12121a", TOG_ON_H="#7fffff",
    GR_GRID="#606370", GR_TEXT="#8a8478",
    CORE_LO="#191922", CORE_MED="#0a8f8f",
    CORE_HI="#00bdbd", CORE_MAX="#00ffff",
    ACCENT="#00ffff", ACCENT_DIM="#0a8f8f",
    WARN="#ffb000", ERR="#ff3b30", LINE="#131319",
)


def _parse_tokens(text: str) -> dict[str, str]:
    """Pull `--name: value;` out of the first :root block."""
    block = re.search(r":root\s*\{(.*?)\}", text, re.S)
    if not block:
        return {}
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1))}


def _over(fg: tuple[int, int, int], alpha: float,
          bg: tuple[int, int, int]) -> str:
    """Composite a translucent token onto a background.

    Qt stylesheets take a flat colour, so `rgba(255,255,255,0.02)` has to be
    resolved against whatever it sits on. Doing it here rather than eyeballing
    a hex value is what keeps the app matching the web build exactly.
    """
    r, g, b = (round(f * alpha + k * (1 - alpha)) for f, k in zip(fg, bg))
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip()
    if value.startswith("#") and len(value) == 7:
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    m = re.match(r"rgba?\(([^)]+)\)", value)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        if len(parts) >= 3:
            return tuple(int(float(p)) for p in parts[:3])
    return None


def _alpha(value: str) -> float:
    m = re.match(r"rgba\(([^)]+)\)", value.strip())
    if not m:
        return 1.0
    parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
    return float(parts[3]) if len(parts) > 3 else 1.0


# Tokens that stay TRANSLUCENT. Qt stylesheets do accept rgba(), so these do
# not need compositing — and must not be composited, because the whole point
# is that the gradient and the dot grid show through the surfaces stacked on
# top. Flattening them is what made the desktop look like a different, matte
# version of the same page.
ALPHA_MAP = {
    "PANEL_A":     "--panel",
    "TINT1_A":     "--tint-1",
    "TINT2_A":     "--tint-2",
    "ACCENT03_A":  "--accent-03",
    "ACCENT08_A":  "--accent-08",
    "DOT_A":       "--dot-grid",
    "GLOW_A":      "--glow-faint",
}

ALPHA_FALLBACK = dict(
    PANEL_A="rgba(255, 255, 255, 5)",
    TINT1_A="rgba(216, 222, 236, 5)",
    TINT2_A="rgba(216, 222, 236, 8)",
    ACCENT03_A="rgba(0, 255, 255, 8)",
    ACCENT08_A="rgba(0, 255, 255, 20)",
    DOT_A="rgba(216, 222, 236, 15)",
    GLOW_A="rgba(0, 255, 255, 20)",
)


def _qt_rgba(value: str) -> str | None:
    """CSS rgba() with 0..1 alpha -> Qt rgba() with 0..255 alpha."""
    rgb = _rgb(value)
    if not rgb:
        return None
    a = round(_alpha(value) * 255)
    return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {a})"


def load_theme() -> dict[str, str]:
    try:
        tokens = _parse_tokens(CSS.read_text())
    except OSError:
        return dict(FALLBACK)
    if not tokens:
        return dict(FALLBACK)

    out = dict(FALLBACK)
    for name, token in TOKEN_MAP.items():
        raw = tokens.get(token)
        if not raw:
            continue
        # --cell-3 is `var(--accent)`; follow one level of indirection.
        ref = re.match(r"var\((--[\w-]+)\)", raw.strip())
        if ref:
            raw = tokens.get(ref.group(1), raw)
        rgb = _rgb(raw)
        if rgb:
            out[name] = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    bg = _rgb(out["BG"]) or (8, 8, 14)

    # Derived surfaces: the web build gets these from alpha compositing, which
    # a Qt stylesheet cannot express.
    panel = tokens.get("--panel", "rgba(255,255,255,0.02)")
    prgb = _rgb(panel) or (255, 255, 255)
    out["BTN_DEF"] = _over(prgb, _alpha(panel), bg)
    out["TOG_OFF"] = out["BTN_DEF"]

    acc03 = tokens.get("--accent-03", "rgba(0,255,255,0.03)")
    argb = _rgb(acc03) or (0, 255, 255)
    out["BTN_HOVER"] = _over(argb, max(_alpha(acc03), 0.06), bg)

    accent = _rgb(out["ACCENT"]) or (0, 255, 255)
    # Hover on an active control lightens toward white, matching the web
    # build's brighter accent-on-hover.
    out["BTN_ACT_H"] = _over((255, 255, 255), 0.5, accent)
    out["TOG_ON"] = out["ACCENT"]
    out["TOG_ON_H"] = out["BTN_ACT_H"]
    out["ACCENT_DIM"] = out.get("CORE_MED", out["ACCENT"])

    out.update(ALPHA_FALLBACK)
    for name, token in ALPHA_MAP.items():
        raw = tokens.get(token)
        if raw:
            q = _qt_rgba(raw)
            if q:
                out[name] = q

    # The page gradient runs --bg-0 -> --bg-1 top to bottom, with a faint
    # accent glow over it. The desktop painted one flat colour.
    for name, token in (("BG_0", "--bg-0"), ("BG_1", "--bg-1"),
                        ("LINE_2", "--line-2"), ("LINE_HOVER", "--line-hover")):
        rgb = _rgb(tokens.get(token, ""))
        if rgb:
            out[name] = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    out.setdefault("BG_0", "#06060a")
    out.setdefault("BG_1", "#0a0a10")
    out.setdefault("LINE_2", "#131319")
    out.setdefault("LINE_HOVER", "#2c2f38")
    return out
