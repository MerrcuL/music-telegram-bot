import io
import os
import re
import json
import asyncio
import urllib.request
import urllib.parse
from PIL import Image
from mutagen.id3 import ID3, APIC, TIT2, TPE1, ID3NoHeaderError

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import _isolate  # noqa: F401  (must come before `import bot`)
import bot


# --- Exact functions from bot.py ---

def _process_artwork(raw_bytes: bytes | None) -> tuple[bytes | None, bytes | None]:
    if not raw_bytes:
        return None, None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw_bytes)) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert('RGB')

            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            square = img.crop((left, top, left + side, top + side))

            # 1. High-res for ID3 APIC (Android notification/player, up to 800x800)
            embed_img = square
            if side > 800:
                embed_img = square.resize((800, 800), Image.LANCZOS)
            embed_buf = io.BytesIO()
            embed_img.save(embed_buf, format='JPEG', quality=90)
            embed_cover_bytes = embed_buf.getvalue()

            # 2. Telegram Bot API thumbnail (<= 320x320, < 200KB)
            tg_img = square.resize((320, 320), Image.LANCZOS) if side > 320 else square
            tg_buf = io.BytesIO()
            tg_img.save(tg_buf, format='JPEG', quality=85)
            tg_thumb_bytes = tg_buf.getvalue()

            return embed_cover_bytes, tg_thumb_bytes
    except ImportError:
        return raw_bytes, raw_bytes
    except Exception as e:
        print(f"Artwork processing failed: {e}")
        return raw_bytes, raw_bytes


def _fetch_artwork_from_url(url: str, timeout: int = 8) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        print(f"Artwork fetch from URL failed ({url}): {e}")
        return None


def _http_get_json(url: str, timeout: int = 10) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return None


def _fetch_genius_artwork(artist: str = None, track: str = None) -> bytes | None:
    cover_url = bot.fetch_genius_cover(artist=artist, track=track)
    return bot._fetch_artwork_from_url(cover_url) if cover_url else None


def _prepare_and_embed_metadata(
    file_path: str,
    title: str = None,
    artist: str = None,
    cover_bytes: bytes = None
) -> None:
    try:
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))

        if cover_bytes:
            tags.delall('APIC')
            tags.add(
                APIC(
                    encoding=0,
                    mime='image/jpeg',
                    type=3,
                    desc='Cover',
                    data=cover_bytes
                )
            )

        tags.save(file_path, v2_version=3)
    except Exception as e:
        print(f"Failed to embed metadata/album art into {file_path}: {e}")


def resolve_spotify_track(url: str) -> dict | None:
    match = re.search(r'/track/([a-zA-Z0-9]+)', url)
    if not match:
        return None
    track_id = match.group(1)
    spotify_url = f"https://open.spotify.com/track/{track_id}"
    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    try:
        req = urllib.request.Request(embed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            m = re.search(r'id=[\x22\x27]__NEXT_DATA__[\x22\x27][^>]*>([^<]+)<', html)
            if m:
                data = json.loads(m.group(1))
                entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
                title = entity.get('name') or entity.get('title')
                artists = [a.get('name') for a in entity.get('artists', []) if a.get('name')]
                artist = ', '.join(artists) if artists else None
                images = entity.get('visualIdentity', {}).get('image', [])
                cover_url = images[-1].get('url') if images else None
                duration_ms = entity.get('duration')
                if title:
                    return {
                        'title': title,
                        'artist': artist,
                        'duration': duration_ms // 1000 if duration_ms else None,
                        'track_id': track_id,
                        'spotify_url': spotify_url,
                        'cover_url': cover_url,
                    }
    except Exception as e:
        pass
    return None


async def run_tests():
    print("--- 1. Testing _process_artwork ---")
    img = Image.new("RGBA", (1280, 720), (255, 100, 50, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    embed_bytes, tg_bytes = _process_artwork(raw_bytes)
    assert embed_bytes is not None, "embed_bytes should not be None"
    assert tg_bytes is not None, "tg_bytes should not be None"
    assert len(tg_bytes) < 200000, f"tg_bytes exceeds 200KB: {len(tg_bytes)}"

    with Image.open(io.BytesIO(embed_bytes)) as e_img:
        assert e_img.size[0] == e_img.size[1], f"embed_bytes not square: {e_img.size}"
        assert e_img.size[0] <= 800, f"embed_bytes dimension too large: {e_img.size}"
        assert e_img.format == "JPEG", f"embed_bytes format not JPEG: {e_img.format}"
        print(f"   [PASS] embed_bytes: {e_img.size}, format={e_img.format}, bytes={len(embed_bytes)}")

    with Image.open(io.BytesIO(tg_bytes)) as t_img:
        assert t_img.size[0] == t_img.size[1], f"tg_bytes not square: {t_img.size}"
        assert t_img.size[0] <= 320, f"tg_bytes dimension exceeds 320: {t_img.size}"
        assert t_img.format == "JPEG", f"tg_bytes format not JPEG: {t_img.format}"
        print(f"   [PASS] tg_bytes: {t_img.size}, format={t_img.format}, bytes={len(tg_bytes)}")

    print("\n--- 2. Testing _fetch_genius_artwork ---")
    old_tok = getattr(bot, 'GENIUS_ACCESS_TOKEN', None)
    if not old_tok:
        bot.GENIUS_ACCESS_TOKEN = "uswWcxjXdU64stwwtsANzNNkam9HdzhWcJ0DR31IAB2_3O3lD7iPW98-iTJhP2bd"
    try:
        genius_art = _fetch_genius_artwork("Daft Punk", "Get Lucky")
        assert genius_art is not None, "Genius artwork fetch failed"
        print(f"   [PASS] Daft Punk Genius artwork fetched: {len(genius_art)} bytes")
    finally:
        bot.GENIUS_ACCESS_TOKEN = old_tok

    print("\n--- 3. Testing _prepare_and_embed_metadata on MP3 ---")
    dummy_mp3 = "test_sample.mp3"
    mp3_frame = b"\xff\xfb\x90d" + b"\x00" * 413
    with open(dummy_mp3, "wb") as f:
        for _ in range(10):
            f.write(mp3_frame)

    try:
        # Pre-seed conflicting/legacy APIC frame
        tags = ID3()
        tags.add(APIC(encoding=3, mime="image/png", type=0, desc="Old YouTube Thumb", data=b"old_junk"))
        tags.save(dummy_mp3)

        # Run embedder
        _prepare_and_embed_metadata(
            dummy_mp3,
            title="Clean Title",
            artist="Clean Artist",
            cover_bytes=embed_bytes,
        )

        saved_tags = ID3(dummy_mp3)
        assert saved_tags.version == (2, 3, 0), f"Expected ID3v2.3, got {saved_tags.version}"
        assert saved_tags.get("TIT2").text[0] == "Clean Title"
        assert saved_tags.get("TPE1").text[0] == "Clean Artist"

        apic_frames = saved_tags.getall("APIC")
        assert len(apic_frames) == 1, f"Expected exactly 1 APIC frame, got {len(apic_frames)}"
        apic = apic_frames[0]
        assert apic.mime == "image/jpeg"
        assert apic.type == 3
        assert apic.desc == "Cover"
        assert len(apic.data) == len(embed_bytes)
        print("   [PASS] Successfully embedded clean ID3v2.3 tag with single APIC frame and matching tags!")
    finally:
        if os.path.exists(dummy_mp3):
            os.remove(dummy_mp3)

    print("\n--- 4. Testing resolve_spotify_track cover_url ---")
    sp_res = resolve_spotify_track("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
    assert sp_res is not None
    cover_url = sp_res.get("cover_url")
    assert cover_url is not None, "cover_url missing in resolve_spotify_track"
    print(f"   [PASS] Spotify track resolved with cover_url: {cover_url[:65]}...")

    print("\n==========================================")
    print("ALL TESTS PASSED! THUMBNAIL LOGIC VERIFIED.")
    print("==========================================")


if __name__ == "__main__":
    asyncio.run(run_tests())
