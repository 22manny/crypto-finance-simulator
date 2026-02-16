# crypto-finance-simulator

This repository now includes a production-ready Python 3.11 CLI tool, `run_pipeline.py`, for generating TikTok assets from a JSON manifest.

## TikTok Asset Pipeline CLI

The pipeline reads a manifest and generates:
- Scene images (PNG) and thumbnail images via Google Gemini Image Generation (`google-genai` SDK)
- Scene voiceover audio (MP3) via ElevenLabs Text-to-Speech from SSML
- Optional concatenated full-language audio files

## Installation

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment Variables

Set API keys as environment variables (never hardcode secrets):

```bash
export GEMINI_API_KEY="your_gemini_key"
export ELEVENLABS_API_KEY="your_elevenlabs_key"
```

## Usage

```bash
python run_pipeline.py /path/to/manifest.json --lang es
python run_pipeline.py /path/to/manifest.json --lang en
python run_pipeline.py /path/to/manifest.json --lang both
```

Optional flags:

```bash
--force
--images-only
--audio-only
--model-image NAME
--log-level INFO|DEBUG
```

## Examples

```bash
python run_pipeline.py ./manifest.json --lang es --log-level DEBUG
python run_pipeline.py ./manifest.json --lang en --audio-only
python run_pipeline.py ./manifest.json --lang both --model-image gemini-2.5-flash-image-preview
python run_pipeline.py ./manifest.json --lang both --force
```

## ffmpeg on Debian 12

For best audio concatenation performance and compatibility:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

If `ffmpeg` is unavailable, the pipeline falls back to `pydub` concatenation.

## Outputs and Logging

- Output root directory is built from `output.base_dir/output.folder_name`
- Missing output folders are created automatically
- Skip/overwrite behavior follows `output.skip_existing` and `--force`
- Run log is written to `run_log.txt` in the output root
- Final summary includes created/skipped/failed asset counts
