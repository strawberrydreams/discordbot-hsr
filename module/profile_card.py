"""프로필 카드 이미지 합성.

Pillow 합성은 CPU 바운드다. 이벤트 루프에서 직접 돌리면 렌더링 동안 봇 전체가
멈추므로, 호출부가 asyncio.to_thread로 넘긴다. 이 모듈은 동기 코드만 담는다.

에셋은 저장소에 넣지 않는다. 캐릭터 아트는 HoYoverse 저작물을 Enka 등이
호스팅하는 것이라 재배포 의무를 지지 않도록 렌더링 시점에 받아 쓰고 버린다.
폰트도 마찬가지로 번들하지 않고 시스템에 설치된 것을 찾아 쓴다.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

CARD_SIZE = (880, 440)
BACKGROUND = (24, 26, 34)
ACCENT = (126, 138, 255)
TEXT = (238, 240, 248)
MUTED = (150, 156, 176)

# 한국어 글리프가 있는 폰트. python:3.12-slim에는 CJK 폰트가 없어 이미지에
# 넣으려면 설치가 필요하다. Dockerfile이 fonts-noto-cjk를 넣는다.
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
)
# 배포판이 경로를 옮겨도 CJK 폰트를 찾아낸다. 고정 후보만 두면 base image가
# 바뀔 때 카드가 조용히 텍스트로 떨어진다.
FONT_SEARCH_ROOTS = ("/usr/share/fonts", "/usr/local/share/fonts")
FONT_NAME_HINTS = ("notosanscjk", "notosanskr", "nanumgothic", "notoserifcjk")


class CardRenderUnavailable(Exception):
    """카드를 그릴 수 없다. 호출부는 텍스트 임베드로 물러선다."""


def _scan_font_roots() -> Optional[Path]:
    for root in FONT_SEARCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in {".ttc", ".ttf", ".otf"}:
                continue
            if any(hint in path.name.lower().replace("-", "") for hint in FONT_NAME_HINTS):
                return path
    return None


def find_font_path() -> Optional[Path]:
    """CARD_FONT_PATH → 알려진 경로 → 폰트 디렉터리 훑기 순. 없으면 None."""
    override = os.getenv("CARD_FONT_PATH")
    candidates = (override, *FONT_CANDIDATES) if override else FONT_CANDIDATES
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return _scan_font_roots()


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError as exc:
        raise CardRenderUnavailable(f"폰트를 열 수 없습니다: {path}") from exc


def _paste_art(canvas: Image.Image, art_bytes: bytes) -> None:
    """오른쪽에 대표 캐릭터 아트를 얹는다. 실패해도 카드 자체는 나가야 한다."""
    with Image.open(io.BytesIO(art_bytes)) as source:
        art = source.convert("RGBA")
        box = (CARD_SIZE[0] // 2, CARD_SIZE[1])
        art.thumbnail(box, Image.LANCZOS)
        canvas.alpha_composite(
            art,
            (CARD_SIZE[0] - art.width, CARD_SIZE[1] - art.height),
        )


def render_card(
    *,
    title: str,
    subtitle: str,
    lines: List[str],
    footer: str,
    art_bytes: Optional[bytes] = None,
    font_path: Optional[Path] = None,
) -> bytes:
    """PNG 바이트를 돌려준다. 동기 함수 — 반드시 스레드에서 호출한다."""
    path = font_path or find_font_path()
    if path is None:
        raise CardRenderUnavailable(
            "한국어 글리프가 있는 폰트를 찾지 못했습니다. CARD_FONT_PATH를 설정하세요."
        )

    canvas = Image.new("RGBA", CARD_SIZE, (*BACKGROUND, 255))
    if art_bytes:
        try:
            _paste_art(canvas, art_bytes)
        except (OSError, ValueError):
            pass  # 아트가 깨져도 텍스트 카드는 나간다

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 8, CARD_SIZE[1]), fill=ACCENT)

    title_font = _load_font(path, 44)
    subtitle_font = _load_font(path, 24)
    body_font = _load_font(path, 26)
    footer_font = _load_font(path, 20)

    left, y = 40, 40
    draw.text((left, y), title, font=title_font, fill=TEXT)
    y += 58
    draw.text((left, y), subtitle, font=subtitle_font, fill=MUTED)
    y += 52

    for line in lines[:6]:
        draw.text((left, y), line, font=body_font, fill=TEXT)
        y += 38

    draw.text((left, CARD_SIZE[1] - 36), footer, font=footer_font, fill=MUTED)

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
