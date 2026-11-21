import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

app = FastAPI(title="手机图片转 MP4")

# 网页直接写在 main.py 中，不再依赖 static 文件夹。
HTML_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>图片转 MP4 视频</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f9;color:#202638;font-family:system-ui,"Noto Sans SC",sans-serif}
main{max-width:620px;margin:24px auto;padding:18px}.card{background:white;border-radius:18px;padding:20px;box-shadow:0 4px 22px #1d2c4410}
h1{font-size:24px;margin:0 0 8px}.tip{color:#667085;font-size:14px;line-height:1.6}
label{display:block;font-weight:650;margin:18px 0 8px}input,textarea,select{width:100%;padding:12px;border:1px solid #d5dae3;border-radius:10px;background:#fff;font-size:16px}
textarea{min-height:110px;resize:vertical}.btn{margin-top:20px;width:100%;border:0;border-radius:12px;background:#2463eb;color:#fff;padding:15px;font-size:17px;font-weight:700}
.btn:disabled{opacity:.55}.status{margin-top:14px;font-size:14px;white-space:pre-wrap}.small{font-size:12px;color:#777;margin-top:14px}
</style></head>
<body><main><section class="card">
<h1>图片转 MP4 视频</h1>
<div class="tip">上传图片，添加字幕和背景音乐，服务器合成标准 MP4 文件。建议先用 2–4 张图片测试。</div>
<form id="form">
<label>① 选择图片（最多 8 张）</label>
<input id="images" type="file" accept="image/jpeg,image/png,image/webp" multiple required>
<label>② 字幕（每行对应一张图片，可留空）</label>
<textarea id="subs" placeholder="第一张图片显示的字幕&#10;第二张图片显示的字幕&#10;第三张图片显示的字幕"></textarea>
<label>③ 每张图片显示时长</label>
<select id="seconds"><option value="3">3 秒</option><option value="4" selected>4 秒</option><option value="5">5 秒</option><option value="6">6 秒</option><option value="8">8 秒</option></select>
<label>④ 背景音乐（可选）</label>
<input id="music" type="file" accept="audio/*">
<button class="btn" id="go" type="submit">开始合成 MP4</button>
<div class="status" id="status"></div>
</form>
<div class="small">免费服务器可能休眠；首次打开需等待唤醒。图片和音乐会上传到服务器临时处理，请勿上传隐私文件。每张图片不超过 12MB，音乐不超过 20MB，视频最长 64 秒。</div>
</section></main>
<script>
const form=document.getElementById('form'), status=document.getElementById('status'), go=document.getElementById('go');
form.addEventListener('submit',async e=>{
 e.preventDefault();const imgs=document.getElementById('images').files;
 if(!imgs.length||imgs.length>8){status.textContent='请选择 1 到 8 张图片';return}
 const fd=new FormData();for(const f of imgs)fd.append('images',f);
 fd.append('subtitles',document.getElementById('subs').value);
 fd.append('seconds',document.getElementById('seconds').value);
 const music=document.getElementById('music').files[0];if(music)fd.append('music',music);
 go.disabled=true;status.textContent='正在上传并合成，请保持页面打开……';
 try{
  const r=await fetch('/render',{method:'POST',body:fd});
  if(!r.ok){let t=await r.text();throw new Error(t)}
  const blob=await r.blob();const url=URL.createObjectURL(blob);
  const a=document.createElement('a');a.href=url;a.download='我的视频.mp4';document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),60000);status.textContent='合成完成！MP4 已开始下载。';
 }catch(err){status.textContent='合成失败：'+err.message}
 finally{go.disabled=false}
});
</script></body></html>
'''


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


@app.get("/health")
async def health():
    return {"ok": True, "format": "MP4 (H.264/AAC)"}


@app.post("/render")
async def render(
    images: list[UploadFile] = File(...),
    subtitles: str = Form(""),
    seconds: float = Form(4.0),
    music: UploadFile | None = File(None),
):
    if not images or len(images) > 8:
        raise HTTPException(status_code=400, detail="请上传 1 到 8 张图片")
    if seconds < 2 or seconds > 8:
        raise HTTPException(status_code=400, detail="每张图片时长请设置为 2 到 8 秒")

    work = Path(tempfile.mkdtemp(prefix="mp4job_"))
    try:
        image_paths = []
        allowed_images = {".jpg", ".jpeg", ".png", ".webp"}

        for i, upload in enumerate(images):
            ext = Path(upload.filename or "").suffix.lower()
            if ext not in allowed_images:
                raise HTTPException(status_code=400, detail="图片仅支持 JPG、PNG、WEBP")
            data = await upload.read()
            if not data:
                raise HTTPException(status_code=400, detail="有图片文件为空，请重新选择")
            if len(data) > 12 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="单张图片不能超过 12MB")
            image_path = work / f"image_{i:02d}{ext}"
            image_path.write_bytes(data)
            image_paths.append(image_path)

        music_path = None
        if music is not None and music.filename:
            ext = Path(music.filename).suffix.lower()
            if ext not in {".mp3", ".m4a", ".aac", ".wav", ".ogg"}:
                raise HTTPException(status_code=400, detail="背景音乐仅支持 MP3、M4A、AAC、WAV、OGG")
            music_data = await music.read()
            if len(music_data) > 20 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="音乐文件不能超过 20MB")
            music_path = work / ("music" + ext)
            music_path.write_bytes(music_data)

        caption_lines = [line.strip() for line in subtitles.splitlines()]
        total_seconds = len(image_paths) * seconds
        if total_seconds > 64:
            raise HTTPException(status_code=400, detail="视频最长 64 秒，请减少图片数量或缩短单张时长")

        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for image_path in image_paths:
            command += ["-loop", "1", "-t", str(seconds), "-i", str(image_path)]
        if music_path:
            command += ["-stream_loop", "-1", "-i", str(music_path)]

        filters = []
        video_labels = []
        font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        for i in range(len(image_paths)):
            caption = caption_lines[i] if i < len(caption_lines) else ""
            caption_file = work / f"caption_{i}.txt"
            caption_file.write_text(caption, encoding="utf-8")
            label = f"v{i}"
            video_labels.append(f"[{label}]")
            filters.append(
                f"[{i}:v]scale=640:480:force_original_aspect_ratio=decrease,"
                f"pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,drawtext=fontfile={font_path}:"
                f"textfile={caption_file}:fontcolor=white:fontsize=26:"
                f"borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-text_h-24,"
                f"format=yuv420p[{label}]"
            )

        filters.append(
            "".join(video_labels)
            + f"concat=n={len(image_paths)}:v=1:a=0[outv]"
        )
        command += ["-filter_complex", ";".join(filters), "-map", "[outv]"]

        if music_path:
            audio_index = len(image_paths)
            command += ["-map", f"{audio_index}:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        else:
            command += ["-an"]

        output_path = work / "video.mp4"
        command += [
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-r", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_path),
        ]

        result = subprocess.run(command, capture_output=True, text=True, timeout=240)
        if result.returncode != 0 or not output_path.exists():
            error_text = (result.stderr or "请减少图片大小后重试")[-1500:]
            raise HTTPException(status_code=500, detail="视频合成失败：" + error_text)

        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename="我的视频.mp4",
            background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=504, detail="合成超时，请减少图片数量或缩短视频")
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
