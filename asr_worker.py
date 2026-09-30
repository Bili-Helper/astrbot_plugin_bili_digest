"""Optional faster-whisper worker. A separate process makes cancellation effective."""

import json
import sys
from pathlib import Path


def main():
    from faster_whisper import WhisperModel

    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    model = WhisperModel(cfg["model"], device=cfg["device"], compute_type=cfg["compute_type"])
    all_segments = []
    for index, path in enumerate(cfg["files"]):
        segments, _ = model.transcribe(path, language=cfg["language"] or None, vad_filter=True)
        offset = index * cfg["segment_seconds"]
        for s in segments:
            all_segments.append({"start": s.start + offset, "end": s.end + offset, "text": s.text})
    print(json.dumps(all_segments, ensure_ascii=False))


if __name__ == "__main__":
    main()
