#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests


class ManifestValidationError(Exception):
    """Raised when the manifest schema is invalid."""


@dataclass
class RunStats:
    created: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] | None = None

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate TikTok images and audio from a manifest file."
    )
    parser.add_argument("manifest", help="Path to manifest.json")
    parser.add_argument("--lang", choices=["es", "en", "both"], required=True)
    parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    parser.add_argument(
        "--images-only", action="store_true", help="Generate images and thumbnails only"
    )
    parser.add_argument("--audio-only", action="store_true", help="Generate audio only")
    parser.add_argument("--model-image", help="Override generation.images.model")
    parser.add_argument(
        "--log-level", default="INFO", choices=["INFO", "DEBUG"], help="Log level"
    )
    args = parser.parse_args()

    if args.images_only and args.audio_only:
        parser.error("--images-only and --audio-only cannot be used together.")

    return args


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise ManifestValidationError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"Invalid JSON in manifest: {exc}") from exc


def require_type(obj: dict[str, Any], key: str, expected: type, path: str) -> Any:
    if key not in obj:
        raise ManifestValidationError(f"Missing key: {path}.{key}")
    value = obj[key]
    if not isinstance(value, expected):
        raise ManifestValidationError(
            f"Invalid type for {path}.{key}: expected {expected.__name__}, got {type(value).__name__}"
        )
    return value


def validate_manifest(manifest: dict[str, Any], requested_lang: str) -> list[str]:
    warnings: list[str] = []

    for key, typ in {
        "manifest_version": str,
        "schema_locked": bool,
        "meta": dict,
        "output": dict,
        "generation": dict,
        "scenes": list,
        "thumbnails": dict,
        "captions": dict,
        "hashtags": dict,
        "pinned_comment": dict,
        "follow_up_idea": dict,
    }.items():
        require_type(manifest, key, typ, "manifest")

    meta = manifest["meta"]
    output = manifest["output"]
    generation = manifest["generation"]
    scenes = manifest["scenes"]
    thumbnails = manifest["thumbnails"]

    require_type(meta, "date", str, "meta")
    require_type(meta, "topic", str, "meta")
    require_type(meta, "topic_slug", str, "meta")
    require_type(meta, "total_duration_s", int, "meta")
    require_type(meta, "total_word_count", int, "meta")
    require_type(meta, "scene_count", int, "meta")
    require_type(meta, "image_count", int, "meta")
    langs = require_type(meta, "languages", list, "meta")
    require_type(meta, "series_branding", str, "meta")
    require_type(meta, "comment_bait_scene", int, "meta")

    for key in ["base_dir", "folder_name", "skip_existing", "paths"]:
        if key == "skip_existing":
            require_type(output, key, bool, "output")
        elif key == "paths":
            require_type(output, key, dict, "output")
        else:
            require_type(output, key, str, "output")

    paths = output["paths"]
    for key in [
        "images",
        "audio_es",
        "audio_en",
        "full_audio_es",
        "full_audio_en",
        "thumbnail_a",
        "thumbnail_b",
    ]:
        require_type(paths, key, str, "output.paths")

    require_type(generation, "audio", dict, "generation")
    require_type(generation, "images", dict, "generation")

    ga = generation["audio"]
    gi = generation["images"]
    require_type(ga, "provider", str, "generation.audio")
    require_type(ga, "model", str, "generation.audio")
    require_type(ga, "voice_name", dict, "generation.audio")
    require_type(ga, "format", str, "generation.audio")
    require_type(ga, "render_full_audio", bool, "generation.audio")
    require_type(ga["voice_name"], "es", str, "generation.audio.voice_name")
    require_type(ga["voice_name"], "en", str, "generation.audio.voice_name")

    require_type(gi, "provider", str, "generation.images")
    require_type(gi, "model", str, "generation.images")
    require_type(gi, "aspect_ratio", str, "generation.images")
    require_type(gi, "format", str, "generation.images")

    if requested_lang == "both":
        if not ("es" in langs and "en" in langs):
            raise ManifestValidationError(
                "Manifest meta.languages must include both 'es' and 'en' for --lang both"
            )
    elif requested_lang not in langs:
        raise ManifestValidationError(
            f"Manifest meta.languages does not include requested language '{requested_lang}'"
        )

    if meta["scene_count"] != len(scenes):
        warnings.append(
            f"meta.scene_count={meta['scene_count']} but len(scenes)={len(scenes)}"
        )
    if meta["image_count"] != len(scenes) + 2:
        warnings.append(
            f"meta.image_count={meta['image_count']} but expected len(scenes)+2={len(scenes)+2}"
        )

    for i, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise ManifestValidationError(f"scenes[{i-1}] must be an object")
        require_type(scene, "scene", int, f"scenes[{i-1}]")
        require_type(scene, "title", str, f"scenes[{i-1}]")
        require_type(scene, "duration_s", int, f"scenes[{i-1}]")
        require_type(scene, "word_count", int, f"scenes[{i-1}]")
        require_type(scene, "transition", str, f"scenes[{i-1}]")
        voiceover = require_type(scene, "voiceover", dict, f"scenes[{i-1}]")
        image = require_type(scene, "image", dict, f"scenes[{i-1}]")
        overlay = require_type(scene, "overlay_text", dict, f"scenes[{i-1}]")

        require_type(voiceover, "es_ssml", str, f"scenes[{i-1}].voiceover")
        require_type(voiceover, "en_ssml", str, f"scenes[{i-1}].voiceover")
        prompt = require_type(image, "prompt", str, f"scenes[{i-1}].image")
        require_type(overlay, "es", str, f"scenes[{i-1}].overlay_text")
        require_type(overlay, "en", str, f"scenes[{i-1}].overlay_text")

        validate_prompt_style(prompt, f"scene {i}", warnings)

    for option_key in ["option_a", "option_b"]:
        option = require_type(thumbnails, option_key, dict, "thumbnails")
        text = require_type(option, "text", dict, f"thumbnails.{option_key}")
        require_type(text, "es", str, f"thumbnails.{option_key}.text")
        require_type(text, "en", str, f"thumbnails.{option_key}.text")
        require_type(option, "psychology", str, f"thumbnails.{option_key}")
        require_type(option, "best_for", str, f"thumbnails.{option_key}")
        require_type(option, "when_to_use", str, f"thumbnails.{option_key}")
        image = require_type(option, "image", dict, f"thumbnails.{option_key}")
        prompt = require_type(image, "prompt", str, f"thumbnails.{option_key}.image")
        validate_prompt_style(prompt, f"thumbnail {option_key}", warnings)

    hashtags = manifest["hashtags"]
    es_tags = require_type(hashtags, "es", list, "hashtags")
    en_tags = require_type(hashtags, "en", list, "hashtags")
    if not all(isinstance(t, str) for t in es_tags + en_tags):
        raise ManifestValidationError("hashtags.es and hashtags.en must contain only strings")

    return warnings


def validate_prompt_style(prompt: str, name: str, warnings: list[str]) -> None:
    prefix = "Vertical 9:16 illustration for TikTok video"
    suffix = (
        "consistent cartoon science explainer style, same color palette, "
        "same illustration style"
    )
    if not prompt.startswith(prefix):
        warnings.append(f"{name} prompt does not start with required prefix")
    if not prompt.endswith(suffix):
        warnings.append(f"{name} prompt does not end with required suffix")


def setup_logger(root_dir: Path, log_level: str) -> logging.Logger:
    logger = logging.getLogger("pipeline")
    logger.setLevel(getattr(logging, log_level))
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    file_handler = logging.FileHandler(root_dir / "run_log.txt", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def with_retries(
    func: Callable[[], Any],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    is_transient: Callable[[Exception], bool] | None = None,
) -> Any:
    attempt = 0
    while True:
        try:
            return func()
        except Exception as exc:
            attempt += 1
            transient = is_transient(exc) if is_transient else True
            if attempt >= retries or not transient:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            time.sleep(delay)


def is_transient_request_error(exc: Exception) -> bool:
    if isinstance(exc, requests.RequestException):
        response = getattr(exc, "response", None)
        if response is None:
            return True
        return response.status_code >= 500 or response.status_code == 429
    return True


def resolve_path(root_dir: Path, template: str, scene_num: int | None = None) -> Path:
    if scene_num is not None:
        relative = template.format(scene=scene_num)
    else:
        relative = template
    return root_dir / relative


def ensure_output_dirs(root_dir: Path, paths: dict[str, str]) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    required_dirs = set()
    for template in paths.values():
        parent = Path(template).parent
        if str(parent) not in ("", "."):
            required_dirs.add(parent)
    for d in required_dirs:
        (root_dir / d).mkdir(parents=True, exist_ok=True)


def should_skip(path: Path, skip_existing: bool, force: bool) -> bool:
    return skip_existing and not force and path.exists()


def extract_image_bytes(response: Any) -> bytes:
    # google-genai SDK response shape can vary by version; support common variants.
    if hasattr(response, "generated_images") and response.generated_images:
        img = response.generated_images[0].image
        for attr in ("image_bytes", "bytes"):
            data = getattr(img, attr, None)
            if data:
                return data
        if hasattr(img, "_image_bytes") and img._image_bytes:
            return img._image_bytes
    if hasattr(response, "images") and response.images:
        img = response.images[0]
        for attr in ("image_bytes", "bytes"):
            data = getattr(img, attr, None)
            if data:
                return data
    raise RuntimeError("Could not extract image bytes from Gemini response")


def generate_image(
    client: Any,
    model: str,
    prompt: str,
    aspect_ratio: str,
    output_file: Path,
) -> None:
    from google.genai import types as genai_types

    def do_call() -> None:
        response = client.models.generate_images(
            model=model,
            prompt=prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=aspect_ratio,
            ),
        )
        output_file.write_bytes(extract_image_bytes(response))

    with_retries(do_call, retries=3, base_delay=1.0)


def elevenlabs_list_voices(api_key: str) -> dict[str, str]:
    def do_call() -> requests.Response:
        resp = requests.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key},
            timeout=45,
        )
        resp.raise_for_status()
        return resp

    resp = with_retries(
        do_call, retries=3, base_delay=1.0, is_transient=is_transient_request_error
    )
    data = resp.json()
    voices = data.get("voices", [])
    if not isinstance(voices, list):
        raise RuntimeError("Invalid voices response from ElevenLabs")
    by_name: dict[str, str] = {}
    for v in voices:
        name = v.get("name")
        voice_id = v.get("voice_id")
        if isinstance(name, str) and isinstance(voice_id, str):
            by_name[name] = voice_id
    return by_name


def resolve_voice_id(voices: dict[str, str], requested: str) -> str:
    if requested in voices:
        return voices[requested]
    lowered = {k.lower(): v for k, v in voices.items()}
    if requested.lower() in lowered:
        return lowered[requested.lower()]
    raise RuntimeError(f"Voice name not found in ElevenLabs: '{requested}'")


def tts_to_mp3(api_key: str, voice_id: str, model: str, ssml_text: str, output_file: Path) -> None:
    def do_call() -> None:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": ssml_text,
                "model_id": model,
            },
            timeout=120,
        )
        resp.raise_for_status()
        output_file.write_bytes(resp.content)

    with_retries(
        do_call, retries=3, base_delay=1.0, is_transient=is_transient_request_error
    )


def concat_with_ffmpeg(inputs: list[Path], output: Path) -> None:
    list_file = output.with_suffix(".concat.txt")
    lines = []
    for p in inputs:
        escaped = p.as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        list_file.unlink(missing_ok=True)


def concat_with_pydub(inputs: list[Path], output: Path) -> None:
    from pydub import AudioSegment

    combined = AudioSegment.empty()
    for p in inputs:
        combined += AudioSegment.from_mp3(p)
    combined.export(output, format="mp3")


def concat_audio(inputs: list[Path], output: Path) -> None:
    if not inputs:
        return
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        concat_with_ffmpeg(inputs, output)
    else:
        concat_with_pydub(inputs, output)


def process_images(
    manifest: dict[str, Any],
    root_dir: Path,
    model_image: str,
    force: bool,
    logger: logging.Logger,
    stats: RunStats,
) -> None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for image generation")

    output = manifest["output"]
    paths = output["paths"]
    skip_existing = output["skip_existing"]
    scenes = manifest["scenes"]
    aspect_ratio = manifest["generation"]["images"]["aspect_ratio"]

    from google import genai

    client = genai.Client(api_key=api_key)

    for scene in scenes:
        scene_num = scene["scene"]
        out_file = resolve_path(root_dir, paths["images"], scene_num)
        label = f"image scene {scene_num}: {out_file}"
        if should_skip(out_file, skip_existing, force):
            logger.info("SKIP  %s", label)
            stats.skipped += 1
            continue
        try:
            generate_image(
                client,
                model_image,
                scene["image"]["prompt"],
                aspect_ratio,
                out_file,
            )
            logger.info("CREATE %s", label)
            stats.created += 1
        except Exception as exc:
            logger.error("FAIL  %s :: %s", label, exc)
            stats.failed += 1
            stats.failures.append(label)

    for opt_key, path_key in (("option_a", "thumbnail_a"), ("option_b", "thumbnail_b")):
        out_file = resolve_path(root_dir, paths[path_key])
        label = f"thumbnail {opt_key}: {out_file}"
        if should_skip(out_file, skip_existing, force):
            logger.info("SKIP  %s", label)
            stats.skipped += 1
            continue
        prompt = manifest["thumbnails"][opt_key]["image"]["prompt"]
        try:
            generate_image(client, model_image, prompt, aspect_ratio, out_file)
            logger.info("CREATE %s", label)
            stats.created += 1
        except Exception as exc:
            logger.error("FAIL  %s :: %s", label, exc)
            stats.failed += 1
            stats.failures.append(label)


def process_audio(
    manifest: dict[str, Any],
    root_dir: Path,
    lang: str,
    force: bool,
    logger: logging.Logger,
    stats: RunStats,
) -> None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is required for audio generation")

    output = manifest["output"]
    paths = output["paths"]
    skip_existing = output["skip_existing"]
    scenes = manifest["scenes"]
    audio_cfg = manifest["generation"]["audio"]

    voices = elevenlabs_list_voices(api_key)
    voice_es = resolve_voice_id(voices, audio_cfg["voice_name"]["es"])
    voice_en = resolve_voice_id(voices, audio_cfg["voice_name"]["en"])

    langs = ["es", "en"] if lang == "both" else [lang]
    scene_inputs: dict[str, list[Path]] = {"es": [], "en": []}

    for scene in sorted(scenes, key=lambda s: s["scene"]):
        scene_num = scene["scene"]
        for this_lang in langs:
            template = paths[f"audio_{this_lang}"]
            out_file = resolve_path(root_dir, template, scene_num)
            label = f"audio {this_lang} scene {scene_num}: {out_file}"
            if should_skip(out_file, skip_existing, force):
                logger.info("SKIP  %s", label)
                stats.skipped += 1
                if out_file.exists():
                    scene_inputs[this_lang].append(out_file)
                continue
            ssml = scene["voiceover"][f"{this_lang}_ssml"]
            voice_id = voice_es if this_lang == "es" else voice_en
            try:
                tts_to_mp3(api_key, voice_id, audio_cfg["model"], ssml, out_file)
                logger.info("CREATE %s", label)
                stats.created += 1
                scene_inputs[this_lang].append(out_file)
            except Exception as exc:
                logger.error("FAIL  %s :: %s", label, exc)
                stats.failed += 1
                stats.failures.append(label)

    if audio_cfg["render_full_audio"]:
        for this_lang in langs:
            inputs = [p for p in scene_inputs[this_lang] if p.exists()]
            out_file = resolve_path(root_dir, paths[f"full_audio_{this_lang}"])
            label = f"full audio {this_lang}: {out_file}"
            if should_skip(out_file, skip_existing, force):
                logger.info("SKIP  %s", label)
                stats.skipped += 1
                continue
            if not inputs:
                logger.error("FAIL  %s :: no scene inputs available", label)
                stats.failed += 1
                stats.failures.append(label)
                continue
            try:
                concat_audio(inputs, out_file)
                logger.info("CREATE %s", label)
                stats.created += 1
            except Exception as exc:
                logger.error("FAIL  %s :: %s", label, exc)
                stats.failed += 1
                stats.failures.append(label)


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)

    try:
        manifest = load_manifest(manifest_path)
        warnings = validate_manifest(manifest, args.lang)
    except ManifestValidationError as exc:
        print(f"Manifest validation error: {exc}", file=sys.stderr)
        return 1

    output_cfg = manifest["output"]
    root_dir = Path(output_cfg["base_dir"]) / output_cfg["folder_name"]
    ensure_output_dirs(root_dir, output_cfg["paths"])
    logger = setup_logger(root_dir, args.log_level)

    for warning in warnings:
        logger.warning("WARN  %s", warning)

    do_images = not args.audio_only
    do_audio = not args.images_only

    if do_images and not os.getenv("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY is missing")
        return 1
    if do_audio and not os.getenv("ELEVENLABS_API_KEY"):
        logger.error("ELEVENLABS_API_KEY is missing")
        return 1

    image_model = args.model_image or manifest["generation"]["images"]["model"]

    stats = RunStats()

    if do_images:
        try:
            process_images(manifest, root_dir, image_model, args.force, logger, stats)
        except Exception as exc:
            logger.error("FAIL  image pipeline startup :: %s", exc)
            stats.failed += 1
            stats.failures.append(f"image pipeline startup: {exc}")

    if do_audio:
        try:
            process_audio(manifest, root_dir, args.lang, args.force, logger, stats)
        except Exception as exc:
            logger.error("FAIL  audio pipeline startup :: %s", exc)
            stats.failed += 1
            stats.failures.append(f"audio pipeline startup: {exc}")

    logger.info(
        "SUMMARY created=%d skipped=%d failed=%d",
        stats.created,
        stats.skipped,
        stats.failed,
    )
    if stats.failures:
        logger.info("FAILURES: %s", "; ".join(stats.failures))

    return 1 if stats.failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
