import os, tempfile, subprocess, shutil
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

app = FastAPI(title="手机图片转 MP4")
ROOT = Path(__file__).parent
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"ok": True, "format": "MP4 (H.264/AAC)"}

@app.post("/render")
async def render(
    images: list[UploadFile] = File(...),
    subtitles: str = Form(""),
    seconds: float = Form(4.0),
    music: UploadFile | None = File(None)
):
    if not images or len(images) > 8:
        raise HTTPException(400, "请上传 1 到 8 张图片")
    if seconds < 2 or seconds > 8:
        raise HTTPException(400, "每张图片时长请设置为 2 到 8 秒")
    work = Path(tempfile.mkdtemp(prefix="mp4job_"))
    try:
        image_paths = []
        for i, upload in enumerate(images):
            ext = Path(upload.filename or "").suffix.lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                raise HTTPException(400, "图片仅支持 JPG、PNG、WEBP")
            data = await upload.read()
            if len(data) > 12 * 1024 * 1024:
                raise HTTPException(400, "单张图片不能超过 12MB")
            p = work / f"image_{i:02d}{ext}"
            p.write_bytes(data)
            image_paths.append(p)

        music_path = None
        if music and music.filename:
            ext = Path(music.filename).suffix.lower()
            if ext not in [".mp3", ".m4a", ".aac", ".wav", ".ogg"]:
                raise HTTPException(400, "背景音乐仅支持 MP3、M4A、AAC、WAV、OGG")
            data = await music.read()
            if len(data) > 20 * 1024 * 1024:
                raise HTTPException(400, "音乐文件不能超过 20MB")
            music_path = work / ("music" + ext)
            music_path.write_bytes(data)

        lines = [x.strip() for x in subtitles.splitlines() if x.strip()]
        total = len(image_paths) * seconds
        if total > 64:
            raise HTTPException(400, "视频最长 64 秒，请减少图片或缩短单张时长")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in image_paths:
            cmd += ["-loop", "1", "-t", str(seconds), "-i", str(p)]
        if music_path:
            cmd += ["-stream_loop", "-1", "-i", str(music_path)]

        filters = []
        concat_inputs = ""
        for i in range(len(image_paths)):
            txt = lines[i] if i < len(lines) else ""
            txtfile = work / f"caption_{i}.txt"
            txtfile.write_text(txt, encoding="utf-8")
            # Noto CJK font is installed in the Docker image.
            filters.append(
                f"[{i}:v]scale=640:480:force_original_aspect_ratio=decrease,"
                f"pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:"
                f"textfile='{txtfile}':fontcolor=white:fontsize=28:"
                f"borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-28,"
                f"format=yuv420p[v{i}]"
            )
            concat_inputs += f"[v{i}]"
        filters.append(f"{concat_inputs}concat=n={len(image_paths)}:v=1:a=0[outv]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[outv]"]
        if music_path:
            audio_index = len(image_paths)
            cmd += ["-map", f"{audio_index}:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        else:
            cmd += ["-an"]
        output = work / "video.mp4"
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-r", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if result.returncode != 0 or not output.exists():
            raise HTTPException(500, "视频合成失败：" + (result.stderr[-1200:] if result.stderr else "请减少图片大小后重试"))
        return FileResponse(
            output, media_type="video/mp4", filename="我的视频.mp4",
            background=BackgroundTask(shutil.rmtree, work, ignore_errors=True)
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(504, "合成超时，请减少图片数量或缩短视频")
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
