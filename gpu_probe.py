from __future__ import annotations

from pathlib import Path
import argparse
import importlib.metadata
import wave


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _write_silence(path: Path, seconds: float = 1.0, sample_rate: int = 16000) -> None:
    frames = int(sample_rate * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe faster-whisper/CTranslate2 CUDA support.")
    parser.add_argument("--model", default="quantumcookie/anime-whisper-ct2-fp16")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--audio", default="/tmp/anime_subtitle_worker_gpu_probe.wav")
    args = parser.parse_args()

    import ctranslate2
    from faster_whisper import WhisperModel

    print("ctranslate2:", _version("ctranslate2"))
    print("faster-whisper:", _version("faster-whisper"))
    print("nvidia-cuda-runtime-cu12:", _version("nvidia-cuda-runtime-cu12"))
    print("nvidia-cublas-cu12:", _version("nvidia-cublas-cu12"))
    print("nvidia-cudnn-cu12:", _version("nvidia-cudnn-cu12"))
    print("cuda devices:", ctranslate2.get_cuda_device_count())
    print("cuda compute types:", sorted(ctranslate2.get_supported_compute_types("cuda")))

    audio_path = Path(args.audio)
    _write_silence(audio_path)
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    segments, info = model.transcribe(
        str(audio_path),
        language="ja",
        task="transcribe",
        vad_filter=True,
        beam_size=1,
        best_of=1,
        word_timestamps=False,
        condition_on_previous_text=False,
        no_speech_threshold=0.95,
    )
    segment_count = sum(1 for _ in segments)
    print("probe language:", getattr(info, "language", None))
    print("probe duration:", getattr(info, "duration", None))
    print("probe segments:", segment_count)
    print("gpu probe ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
