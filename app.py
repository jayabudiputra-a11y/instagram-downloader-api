import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Portfolio Instagram Downloader")


class DownloadRequest(BaseModel):
    url: str
    type: str = "video"


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
        "ffmpeg": shutil.which("ffmpeg") or "missing"
    }


@app.post("/download")
def download_media(request: DownloadRequest):
    url = request.url.strip()
    media_type = request.type.strip().lower()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Instagram URL is required."
        )

    validate_instagram_url(url)

    if media_type != "video":
        raise HTTPException(
            status_code=400,
            detail="This endpoint currently supports video downloads."
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="instagram-"))
    output_template = str(temp_dir / "%(id)s.%(ext)s")

    command = [
        "yt-dlp",
        "--no-playlist",
        "--restrict-filenames",

        "-f",
        "bestvideo[height>=1080]+bestaudio/bestvideo+bestaudio/best",

        "-S",
        "res,fps,br",

        "--merge-output-format",
        "mp4",

        "-o",
        output_template,
        url,
    ]

    print("RUNNING YT-DLP:")
    print(" ".join(command), flush=True)

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )

        print("YT-DLP OUTPUT:", flush=True)
        print(result.stdout, flush=True)
        print(f"YT-DLP RETURN CODE: {result.returncode}", flush=True)

        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail="Instagram download failed."
            )

        mp4_files = list(temp_dir.glob("*.mp4"))

        if not mp4_files:
            raise HTTPException(
                status_code=502,
                detail="yt-dlp completed but no MP4 was produced."
            )

        video = mp4_files[0]

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,channels,sample_rate",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1",
                str(video),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

        print("FINAL FILE PROBE:", flush=True)
        print(probe.stdout, flush=True)

        if "codec_type=video" not in probe.stdout:
            raise HTTPException(
                status_code=502,
                detail="Final MP4 does not contain a video stream."
            )

        if "codec_type=audio" not in probe.stdout:
            raise HTTPException(
                status_code=502,
                detail="Final MP4 does not contain an audio stream."
            )

        return FileResponse(
            path=str(video),
            media_type="video/mp4",
            filename="instagram-video.mp4",
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Download timed out."
        )
