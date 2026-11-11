# 抖音视频图文笔记

给一个抖音链接，提炼成简单易懂的中文笔记。详细讲解、时间戳和证据截图另存文档。

适用于支持技能、命令执行和看图的 AI Agent。音频在本机转录，Agent 结合全文和画面完成总结；不需要单独购买转录或总结接口。

## 输出什么

| 文件 | 用途 |
| --- | --- |
| `笔记.md` | 核心结论、关键方法、必要参数，快速看懂。 |
| `详细资料.md` | 完整讲解、回看时间、原始截图与待核实信息。 |
| `完整转录.md`、`字幕.srt` | 保留本地语音识别原稿。 |
| `assets/`、`来源.json` | 本地配图与来源信息，整个目录可移入知识库。 |

主笔记按信息价值重新组织，不逐段复述视频，不堆时间戳。图片仅在有助于理解时出现；证据性截图留在详细资料。

## 安装技能

将仓库克隆到 Agent 的技能目录。例如，Windows 上安装到 Codex：

```powershell
git clone https://github.com/worldprogramer/douyin-video-notes.git "$env:USERPROFILE\.codex\skills\douyin-video-notes"
```

若已设置 `CODEX_HOME`，使用其 `skills` 子目录。其他 Agent 使用各自支持的技能目录。然后在支持该技能的会话里说：

> 使用 douyin-video-notes，整理这个抖音链接。提炼核心信息，写得简单清晰，详细信息另存：……

支持短链接、完整分享文案和本地视频。多条视频由 Agent 分别处理；不抓取账号主页、直播或图文合集。

## 准备运行环境

需要 Python 3.11 或 3.12、FFmpeg 与 ffprobe，以及本地 CTranslate2 格式的 Whisper 模型。已有环境可直接复用，不必重新安装。

Windows 首次安装时，在仓库目录运行：

```powershell
.\scripts\bootstrap.ps1 -Workspace (Get-Location).Path -Python python
$notesPython = '.\.douyin-notes-runtime\venv\Scripts\python.exe'
```

脚本只创建项目内的独立虚拟环境，不修改系统搜索路径。FFmpeg 从[官方下载页](https://ffmpeg.org/download.html)选择对应平台构建；可放入搜索路径，或使用 `DOUYIN_NOTES_FFMPEG`、`DOUYIN_NOTES_FFPROBE` 指定程序路径。

首次下载模型到项目内（已经有模型时跳过）：

```powershell
& $notesPython -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='.douyin-notes-runtime/models/whisper-small', allow_patterns=['config.json','model.bin','tokenizer.json','vocabulary.*'])"
& $notesPython .\scripts\douyin_notes.py doctor
```

模型下载使用 Hugging Face 的[本地目录下载接口](https://huggingface.co/docs/huggingface_hub/guides/download#download-files-to-a-local-folder)。转录时只读取本地模型，不自动联网下载权重。更大的模型、显卡配置及独立 FFmpeg 压缩包安装见[环境说明](references/setup.md)。

## 处理流程

1. 获取完整视频。分享页不可用时尝试可选下载器，再由 Agent 使用正常播放页面获取媒体。
2. FFmpeg 提取音轨，本地 faster-whisper 转录并保留时间与置信度。
3. 按时间和场景变化截图，生成多页时间戳拼图，先看全片，再核对关键原图。
4. Agent 分别编写简明笔记与详细资料，发布为可移动的 Markdown 文件夹。

```powershell
& $notesPython .\scripts\douyin_notes.py prepare `
  --input '抖音链接或本地视频路径' --job '.\work\video-note' --language zh

# Agent 完整阅读转录与拼图后，按 references/note-plan.md 编写 note-plan.json。
& $notesPython .\scripts\douyin_notes.py publish `
  --job '.\work\video-note' --plan '.\work\video-note\note-plan.json' --output '.\outputs\video-note'
```

`prepare` 负责准备证据，`publish` 渲染已写好的计划；脚本本身不调用大模型生成摘要。Agent 的完整执行规则见 [SKILL.md](SKILL.md)。

## 使用边界

- 抖音接口和页面可能变化，不能保证所有链接都能直接下载。浏览器备用路线需要 Agent 具有相应能力；登录、验证码无法自动完成时，可改用本地文件。
- 转录在本地运行；看图和整理使用当前会话模型，因此不是全流程离线方案。
- 默认 CPU INT8；可显式选择显卡或自动回退。低置信度转录、数字和专有名词仍需结合原声或截图核对。
- 只处理你有权访问和使用的视频。仓库不包含视频、模型权重、浏览器凭据或 FFmpeg 二进制。

## 验证与参考

已在 Windows 上跑通真实中文视频的下载、音视频合流、本地转录、拼图及双文档输出；另验证了无音轨、缓存续跑、时间边界和截图来源校验。已完成素材不因重跑而重新转录，改变转录设置时需新工作目录或明确重算。

本地回归测试：

```powershell
python -m unittest discover -s tests -v
```

流程参考了 [video-fetcher-to-markdown](https://github.com/JimmySadek/video-fetcher-to-markdown)、[dy-note](https://github.com/Rimagination/dy-note) 和 [douyin-video-parser](https://github.com/ldsoy/douyin-video-parser)；下载器未复制这些仓库的签名实现。媒体处理依赖 [FFmpeg](https://ffmpeg.org/)，本地转录使用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)。

本项目采用 [MIT 许可](LICENSE)。依赖软件、模型和视频内容遵循各自许可。
