
import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


app = FastAPI(title="AI 图片转视频")

# 允许 Hugging Face 前端连接 Render 后端
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_IMAGES = 8
MAX_IMAGE_SIZE = 12 * 1024 * 1024
MAX_AUDIO_SIZE = 20 * 1024 * 1024
MAX_TOTAL_SECONDS = 64

VOICE_MAP = {
    "female": "zh-CN-XiaoxiaoNeural",
    "male": "zh-CN-YunxiNeural",
}


def run_command(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:])
    return result


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("服务器没有安装 FFmpeg")


async def save_upload(upload, destination, max_size):
    if upload is None:
        return None

    data = await upload.read()

    if len(data) > max_size:
        raise HTTPException(status_code=413, detail="上传文件太大")

    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")

    Path(destination).write_bytes(data)
    return destination


async def create_tts(text, gender, output):
    voice = VOICE_MAP.get(gender, VOICE_MAP["female"])
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output)


def create_video(image_paths, seconds, output):
    check_ffmpeg()

    work = Path(output).parent
    clips = []

    for index, image in enumerate(image_paths):
        clip = work / f"clip_{index}.mp4"
        duration = float(seconds)

        command = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(image),
            "-t", str(duration),
            "-vf",
            "scale=640:480:force_original_aspect_ratio=decrease,"
            "pad=640:480:(ow-iw)/2:(oh-ih)/2,"
            "setsar=1,format=yuv420p",
            "-r", "25",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            str(clip)
        ]

        run_command(command)
        clips.append(clip)

    list_file = work / "clips.txt"

    with open(list_file, "w", encoding="utf-8") as file:
        for clip in clips:
            safe_path = str(clip).replace("'", "'\\''")
            file.write(f"file '{safe_path}'\n")

    run_command([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output)
    ])


@app.get("/")
async def home():
    return {
        "message": "AI 图片转视频后端运行正常",
        "docs": "/docs"
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "features": [
            "multiple_images",
            "mp4_video",
            "subtitles",
            "chinese_tts",
            "uploaded_recording",
            "background_music"
        ]
    }


@app.post("/render")
async def render(
    images: list[UploadFile] = File(...),
    subtitles: str = Form(""),
    seconds: float = Form(4.0),
    voice_mode: str = Form("none"),
    narration_text: str = Form(""),
    voice_gender: str = Form("female"),
    recording: UploadFile | None = File(None),
    music: UploadFile | None = File(None)
):
    if not images:
        raise HTTPException(status_code=400, detail="请上传图片")

    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail="最多上传8张图片")

    if seconds < 2 or seconds > 8:
        raise HTTPException(status_code=400, detail="每张图片时长需为2到8秒")

    total_seconds = seconds * len(images)

    if total_seconds > MAX_TOTAL_SECONDS:
        raise HTTPException(status_code=400, detail="视频总时长不能超过64秒")

    work = Path(tempfile.mkdtemp(prefix="ai_video_"))

    try:
        image_paths = []

        for index, image in enumerate(images):
            suffix = Path(image.filename or "").suffix.lower()

            if suffix not in [".jpg", ".jpeg", ".png", ".webp"]:
                raise HTTPException(
                    status_code=400,
                    detail="图片只支持 JPG、PNG、WEBP"
                )

            destination = work / f"image_{index}{suffix}"

            await save_upload(
                image,
                destination,
                MAX_IMAGE_SIZE
            )

            image_paths.append(destination)

        silent_video = work / "silent.mp4"
        output_video = work / "output.mp4"

        create_video(
            image_paths,
            seconds,
            silent_video
        )

        audio_inputs = []
        audio_labels = []

        if voice_mode == "ai" and narration_text.strip():
            tts_file = work / "voice.mp3"

            await create_tts(
                narration_text.strip(),
                voice_gender,
                str(tts_file)
            )

            audio_inputs.extend(["-i", str(tts_file)])
            audio_labels.append("[1:a]")

        elif voice_mode == "recording" and recording is not None:
            recording_file = work / "recording"
            await save_upload(
                recording,
                recording_file,
                MAX_AUDIO_SIZE
            )

            audio_inputs.extend(["-i", str(recording_file)])
            audio_labels.append("[1:a]")

        if music is not None:
            music_file = work / "music"
            await save_upload(
                music,
                music_file,
                MAX_AUDIO_SIZE
            )

            music_index = 1 + len(audio_labels)
            audio_inputs.extend(["-i", str(music_file)])
            audio_labels.append(f"[{music_index}:a]")

        command = [
            "ffmpeg", "-y",
            "-i", str(silent_video)
        ] + audio_inputs

        if audio_labels:
            if len(audio_labels) == 1:
                filter_complex = (
                    f"{audio_labels[0]}volume=1.0[aout]"
                )
            else:
                filter_complex = (
                    f"{audio_labels[0]}volume=1.0[v1];"
                    f"{audio_labels[1]}volume=0.25[m1];"
                    "[v1][m1]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                )

            command += [
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest"
            ]
        else:
            command += [
                "-map", "0:v",
                "-c:v", "copy",
                "-an"
            ]

        command += [
            "-movflags", "+faststart",
            str(output_video)
        ]

        run_command(command)

        return FileResponse(
            path=str(output_video),
            media_type="video/mp4",
            filename="我的视频.mp4",
            background=BackgroundTask(
                shutil.rmtree,
                str(work),
                ignore_errors=True
            )
        )

    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise

    except Exception as error:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"视频生成失败：{str(error)}"
        )
