import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel


app = FastAPI(title="Portfolio Instagram Downloader")


class DownloadRequest(BaseModel):
    url: str
    type: str = "video"
    audio_url: str | None = None
    media_url: str | None = None


def validate_instagram_url(url: str):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    allowed = {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
    }

    if host not in allowed and not host.endswith(".instagram.com"):
        raise HTTPException(
            status_code=400,
            detail="Only Instagram URLs are allowed."
        )


def validate_audio_url(url: str):
    if not url:
        return

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    allowed_hosts = (
        "fbcdn.net",
        "instagram.com",
        "cdninstagram.com",
    )

    if parsed.scheme not in ("http", "https") or not any(
        host == h or host.endswith("." + h) for h in allowed_hosts
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid Instagram audio URL host."
        )


def validate_media_url(url: str):
    if not url:
        raise HTTPException(
            status_code=400,
            detail="Media URL is required."
        )

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    allowed_hosts = (
        "fbcdn.net",
        "instagram.com",
        "cdninstagram.com",
    )

    if parsed.scheme not in ("http", "https") or not any(
        host == h or host.endswith("." + h) for h in allowed_hosts
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid Instagram media URL host."
        )


def prepare_instagram_cookies(temp_dir: Path):
    encoded = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()

    if not encoded:
        print("INSTAGRAM_COOKIES_B64: MISSING", flush=True)
        return None

    try:
        cookie_path = temp_dir / "instagram-cookies.txt"
        cookie_bytes = base64.b64decode(encoded, validate=True)
        cookie_path.write_bytes(cookie_bytes)

        print(
            f"INSTAGRAM COOKIE FILE CREATED: {cookie_path}",
            flush=True
        )
        print(
            f"COOKIE FILE SIZE: {cookie_path.stat().st_size} bytes",
            flush=True
        )

        return cookie_path

    except Exception as exc:
        print(
            f"FAILED TO CREATE INSTAGRAM COOKIE FILE: {exc}",
            flush=True
        )
        raise HTTPException(
            status_code=500,
            detail="Instagram cookie configuration is invalid."
        )


@app.get("/")
def root():
    return {
        "service": "portfolio-instagram-downloader",
        "status": "online"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "yt_dlp": shutil.which("yt-dlp") or "missing",
        "ffmpeg": shutil.which("ffmpeg") or "missing",
        "ffprobe": shutil.which("ffprobe") or "missing",
        "instagram_cookies": (
            "configured"
            if os.getenv("INSTAGRAM_COOKIES_B64", "").strip()
            else "missing"
        ),
    }


def run_command(command, cwd=None, suppress_output=False):
    print("RUNNING COMMAND", flush=True)
    print(f"COMMAND ARGUMENTS REDACTED (count={len(command)})", flush=True)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=False,
        cwd=str(cwd) if cwd else None,
    )

    if not suppress_output:
        output_length = len(result.stdout or "")
        output_lines = len((result.stdout or "").splitlines())
        print(
            f"COMMAND OUTPUT REDACTED (characters={output_length}, lines={output_lines})",
            flush=True,
        )

    print(f"RETURN CODE: {result.returncode}", flush=True)
    return result


def parse_yt_dlp_json(raw: str):
    raw = (raw or "").strip()

    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()

    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
            if isinstance(value, dict) and (
                "id" in value or "entries" in value or "_type" in value
            ):
                return value
        except json.JSONDecodeError:
            continue

    raise ValueError("No valid yt-dlp JSON object was found.")


def extract_carousel_items(temp_dir: Path):
    """Parse Instagram's captured carousel JSON without logging signed media URLs."""
    for dump_path in temp_dir.glob("*.dump"):
        try:
            raw = dump_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        search_offset = 0

        while True:
            key_index = raw.find('"carousel_media"', search_offset)
            if key_index < 0:
                break

            colon_index = raw.find(":", key_index + len('"carousel_media"'))
            if colon_index < 0:
                break

            array_start = colon_index + 1
            while array_start < len(raw) and raw[array_start].isspace():
                array_start += 1

            search_offset = key_index + 1

            if array_start >= len(raw) or raw[array_start] != "[":
                continue

            depth = 0
            in_string = False
            escaped = False
            array_end = -1

            for index in range(array_start, len(raw)):
                char = raw[index]

                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue

                if char == '"':
                    in_string = True
                elif char == "[":
                    depth += 1
                elif char == "]":
                    depth -= 1
                    if depth == 0:
                        array_end = index
                        break

            if array_end < array_start:
                continue

            try:
                items = json.loads(raw[array_start:array_end + 1])
            except json.JSONDecodeError:
                continue

            if not isinstance(items, list) or len(items) < 2:
                continue

            results = []

            for position, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue

                image_candidates = [
                    candidate
                    for candidate in (
                        (item.get("image_versions2") or {}).get("candidates") or []
                    )
                    if isinstance(candidate, dict) and candidate.get("url")
                ]

                video_candidates = [
                    candidate
                    for candidate in (item.get("video_versions") or [])
                    if isinstance(candidate, dict) and candidate.get("url")
                ]

                def resolution_score(candidate):
                    try:
                        return int(candidate.get("width") or 0) * int(
                            candidate.get("height") or 0
                        )
                    except (TypeError, ValueError):
                        return 0

                best_image = max(image_candidates, key=resolution_score, default={})
                best_video = max(video_candidates, key=resolution_score, default={})

                media_type = item.get("media_type")
                is_video = media_type == 2 or "Video" in str(
                    item.get("__typename") or ""
                )
                media_url = (
                    best_video.get("url") if is_video else best_image.get("url")
                )

                thumbnail = (
                    item.get("display_uri")
                    or best_image.get("url")
                    or item.get("thumbnail_src")
                    or ""
                )

                results.append({
                    "position": position,
                    "id": str(item.get("code") or item.get("id") or ""),
                    "type": "video" if is_video else "image",
                    "media_type": media_type,
                    "width": item.get("original_width"),
                    "height": item.get("original_height"),
                    "thumbnail": thumbnail,
                    "image_url": best_image.get("url") or thumbnail,
                    "video_url": best_video.get("url") or "",
                    "media_url": media_url or "",
                    "has_video": bool(video_candidates),
                    "has_audio": False,
                })

            if len(results) >= 2:
                return results

    return []


def probe_file(media: Path):
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,channels,sample_rate",
            "-show_entries",
            "format=duration,size,format_name",
            "-of",
            "default=noprint_wrappers=1",
            str(media),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    print("FINAL FILE PROBE:", flush=True)
    print(probe.stdout, flush=True)

    return probe.stdout


def download_external_audio(audio_url: str, output: Path):
    print("DOWNLOADING EXPLICIT INSTAGRAM AUDIO URL", flush=True)
    print(
        f"AUDIO URL LENGTH: {len(audio_url)}",
        flush=True
    )

    request = urllib.request.Request(
        audio_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/153.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            data = response.read()

        print(
            f"AUDIO HTTP STATUS: {status}",
            flush=True
        )
        print(
            f"AUDIO DOWNLOADED BYTES: {len(data)}",
            flush=True
        )

        if status != 200:
            raise RuntimeError(
                f"Audio HTTP status was {status}"
            )

        if len(data) < 1000:
            raise RuntimeError(
                "Audio response is unexpectedly small."
            )

        output.write_bytes(data)

        print(
            f"AUDIO FILE CREATED: {output}",
            flush=True
        )

    except Exception as exc:
        print(
            f"AUDIO DOWNLOAD FAILED: {exc}",
            flush=True
        )
        raise HTTPException(
            status_code=502,
            detail=f"Instagram audio download failed: {exc}"
        )


@app.get("/metadata")
def metadata(url: str):
    url = url.strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Instagram URL is required."
        )

    validate_instagram_url(url)

    temp_dir = Path(
        tempfile.mkdtemp(prefix="instagram-metadata-")
    )

    cookie_path = None

    try:
        cookie_path = prepare_instagram_cookies(temp_dir)
        print("========================================", flush=True)
        print("INSTAGRAM METADATA EXTRACTION", flush=True)
        print("========================================", flush=True)

        command = [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--dump-single-json",
            "--write-pages",
            "--no-check-certificates",
        ]

        if cookie_path:
            command.extend([
                "--cookies",
                str(cookie_path),
            ])

        command.append(url)

        result = run_command(
            command,
            cwd=temp_dir,
            suppress_output=True,
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail="Instagram metadata extraction failed."
            )

        raw = result.stdout.strip()

        if not raw:
            raise HTTPException(
                status_code=502,
                detail="Instagram metadata response was empty."
            )

        try:
            data = parse_yt_dlp_json(raw)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="yt-dlp returned invalid JSON."
            )

        media_items = extract_carousel_items(temp_dir)
        formats = []

        video_url = ""
        audio_url = ""

        best_video_score = -1
        best_audio_score = -1

        for fmt in data.get("formats", []):
            vcodec = str(fmt.get("vcodec") or "none")
            acodec = str(fmt.get("acodec") or "none")
            format_url = str(fmt.get("url") or "")

            item = {
                "format_id": str(fmt.get("format_id") or ""),
                "ext": str(fmt.get("ext") or ""),
                "resolution": str(fmt.get("resolution") or ""),
                "width": fmt.get("width"),
                "height": fmt.get("height"),
                "fps": fmt.get("fps"),
                "vcodec": vcodec,
                "acodec": acodec,
                "protocol": str(fmt.get("protocol") or ""),
                "has_video": vcodec != "none",
                "has_audio": acodec != "none",
            }

            formats.append(item)

            if format_url and acodec != "none" and vcodec == "none":
                audio_score = float(fmt.get("abr") or 0)

                if audio_score > best_audio_score:
                    best_audio_score = audio_score
                    audio_url = format_url

            if format_url and vcodec != "none" and acodec == "none":
                width = int(fmt.get("width") or 0)
                height = int(fmt.get("height") or 0)
                tbr = float(fmt.get("tbr") or 0)

                video_score = (
                    float(width * height)
                    + tbr
                )

                if video_score > best_video_score:
                    best_video_score = video_score
                    video_url = format_url

        if not audio_url:
            # Railway Instagram extraction currently exposes video-only
            # DASH formats for this reel. Do not incorrectly expose a
            # video-only URL as audio_url.
            print(
                "NO DIRECT AUDIO FORMAT FOUND IN RAILWAY YT-DLP EXTRACTION",
                flush=True
            )

            combined_url = str(data.get("url") or "")
            combined_acodec = str(data.get("acodec") or "none")
            combined_vcodec = str(data.get("vcodec") or "none")

            if (
                combined_url
                and combined_acodec != "none"
                and combined_vcodec != "none"
            ):
                video_url = combined_url

        return {
            "ok": True,
            "extractor": data.get("extractor") or "Instagram",
            "id": data.get("id") or "",
            "title": data.get("title") or "",
            "uploader": data.get("uploader") or data.get("uploader_id") or "",
            "thumbnail": data.get("thumbnail") or "",
            "duration": data.get("duration"),
            "width": data.get("width"),
            "height": data.get("height"),
            "ext": data.get("ext") or "mp4",
            "webpage_url": data.get("webpage_url") or url,
            "video_url": video_url,
            "audio_url": audio_url,
            "image_url": data.get("thumbnail") or "",
            "is_carousel": len(media_items) > 1,
            "media_items": media_items,
            "formats": formats,
        }

    finally:
        if cookie_path and cookie_path.exists():
            cookie_path.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.post("/download")
def download_media(request: DownloadRequest):
    url = request.url.strip()
    media_type = request.type.strip().lower()
    audio_url = (request.audio_url or "").strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Instagram URL is required."
        )

    validate_instagram_url(url)

    if media_type == "image":
        media_url = (request.media_url or "").strip()
        validate_media_url(media_url)

        temp_dir = Path(tempfile.mkdtemp(prefix="instagram-image-"))
        image_output = temp_dir / "instagram-image.bin"

        image_request = urllib.request.Request(
            media_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/153.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )

        try:
            with urllib.request.urlopen(image_request, timeout=60) as response:
                content_type = (
                    response.headers.get_content_type() or "application/octet-stream"
                )
                image_bytes = response.read()

            if len(image_bytes) < 100:
                raise HTTPException(
                    status_code=502,
                    detail="Instagram image response was unexpectedly small."
                )

            if not (
                image_bytes.startswith(b"\xff\xd8\xff")
                or image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                or (
                    len(image_bytes) >= 12
                    and image_bytes[:4] == b"RIFF"
                    and image_bytes[8:12] == b"WEBP"
                )
            ):
                raise HTTPException(
                    status_code=502,
                    detail="Instagram response is not a supported image file."
                )

            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }.get(content_type, ".img")

            image_output = temp_dir / ("instagram-image" + extension)
            image_output.write_bytes(image_bytes)

            return FileResponse(
                path=str(image_output),
                media_type=content_type,
                filename="instagram-image" + extension,
                background=BackgroundTask(
                    shutil.rmtree, temp_dir, ignore_errors=True
                ),
            )

        except HTTPException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(
                status_code=502,
                detail="Instagram image download failed."
            )

    if media_type != "video":
        raise HTTPException(
            status_code=400,
            detail="Supported media types are image and video."
        )

    if not audio_url:
        raise HTTPException(
            status_code=400,
            detail=(
                "audio_url is required. "
                "The Instagram audio stream must be discovered "
                "separately and supplied to the API."
            )
        )

    validate_audio_url(audio_url)

    temp_dir = Path(
        tempfile.mkdtemp(prefix="instagram-")
    )

    video_output = temp_dir / "instagram-video.mp4"
    audio_output = temp_dir / "instagram-audio.m4a"
    merged_output = temp_dir / "instagram-video-audio.mp4"

    cookie_path = None
    response_created = False

    try:
        cookie_path = prepare_instagram_cookies(temp_dir)
        print("========================================", flush=True)
        print("1. DOWNLOAD VIDEO FROM RAILWAY", flush=True)
        print("========================================", flush=True)

        command = [
            "yt-dlp",
            "--no-playlist",
            "--restrict-filenames",
        ]

        if cookie_path:
            command.extend([
                "--cookies",
                str(cookie_path),
            ])

        command.extend([
            "-f",
            "bestvideo/best",
            "--merge-output-format",
            "mp4",
            "-o",
            str(video_output),
            url,
        ])

        result = run_command(command)

        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail="Instagram video download failed."
            )

        if not video_output.exists():
            raise HTTPException(
                status_code=502,
                detail="Video file was not created."
            )

        print("========================================", flush=True)
        print("2. DOWNLOAD EXPLICIT AUDIO", flush=True)
        print("========================================", flush=True)

        download_external_audio(
            audio_url,
            audio_output
        )

        print("========================================", flush=True)
        print("3. VERIFY INPUTS", flush=True)
        print("========================================", flush=True)

        video_probe = probe_file(video_output)
        audio_probe = probe_file(audio_output)

        if "codec_type=video" not in video_probe:
            raise HTTPException(
                status_code=502,
                detail="Downloaded video does not contain a video stream."
            )

        if "codec_type=audio" not in audio_probe:
            raise HTTPException(
                status_code=502,
                detail="Downloaded audio does not contain an audio stream."
            )

        print("========================================", flush=True)
        print("4. MERGE VIDEO + AUDIO", flush=True)
        print("========================================", flush=True)

        merge_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_output),
            "-i",
            str(audio_output),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(merged_output),
        ]

        merge_result = run_command(merge_command)

        if merge_result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail="FFmpeg video/audio merge failed."
            )

        if not merged_output.exists():
            raise HTTPException(
                status_code=502,
                detail="Merged MP4 was not created."
            )

        print("========================================", flush=True)
        print("5. FINAL PROBE", flush=True)
        print("========================================", flush=True)

        final_probe = probe_file(merged_output)

        if "codec_type=video" not in final_probe:
            raise HTTPException(
                status_code=502,
                detail="Final MP4 does not contain video."
            )

        if "codec_type=audio" not in final_probe:
            raise HTTPException(
                status_code=502,
                detail="Final MP4 does not contain audio."
            )

        print(
            "FINAL RESULT: VIDEO + AUDIO",
            flush=True
        )

        response = FileResponse(
            path=str(merged_output),
            media_type="video/mp4",
            filename="instagram-video.mp4",
            background=BackgroundTask(
                shutil.rmtree, temp_dir, ignore_errors=True
            ),
        )
        response_created = True
        return response

    finally:
        if cookie_path and cookie_path.exists():
            cookie_path.unlink(missing_ok=True)
        if not response_created:
            shutil.rmtree(temp_dir, ignore_errors=True)




