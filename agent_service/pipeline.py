"""
Voice-aware transcription pipeline.

Pipeline: URL -> audio file -> Whisper (transcribe + optional translate) -> Prompt-Guard injection scan.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, asdict
from typing import Optional, List
from urllib.parse import urlparse

import yt_dlp
import httpx
import torch
from faster_whisper import WhisperModel
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# Use large-v3 (not turbo) — turbo was distilled for transcription and dropped
# translation capability for low-resource languages.
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
PROMPT_GUARD_MODEL_NAME = os.environ.get("PROMPT_GUARD_MODEL", "meta-llama/Prompt-Guard-86M")
# High bar for auto-flag without keyword corroboration — Prompt-Guard-86M is English-tuned
# and produces false positives on some non-English content.
INJECTION_SCORE_THRESHOLD = float(os.environ.get("INJECTION_SCORE_THRESHOLD", "0.9"))
MAX_AUDIO_DURATION_SEC = int(os.environ.get("MAX_AUDIO_DURATION_SEC", "1800"))  # 30 min cap

# Min characters before we trust Prompt-Guard on a segment. The model overfires
# on short imperative/declarative fragments (e.g. "of digital formats." → 0.9999)
# because it was trained on full-context prompts, not 3-word fragments.
MIN_TEXT_LEN_FOR_PG = int(os.environ.get("MIN_TEXT_LEN_FOR_PG", "40"))

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"

# Sites where yt-dlp is the only viable fetcher. If yt-dlp fails on one of these
# (e.g. YouTube bot wall, age gate, geo block), do NOT silently fall back to
# direct HTTP download — surface the real error to the caller.
_YTDLP_NATIVE_HOSTS = re.compile(
    r"(?:^|\.)("
    r"youtube\.com|youtu\.be|music\.youtube\.com|"
    r"soundcloud\.com|vimeo\.com|spotify\.com|"
    r"twitch\.tv|tiktok\.com|dailymotion\.com|bilibili\.com|"
    r"x\.com|twitter\.com|facebook\.com|instagram\.com"
    r")$", re.IGNORECASE
)

# Path to a Netscape-format cookies.txt file. Set this if YouTube rate-limits
# your IP. Export from a logged-in browser session.
YT_COOKIES_FILE = os.environ.get("YT_COOKIES_FILE", "").strip() or None


def _is_audio_content_type(ct: str) -> bool:
    """Returns True if the HTTP Content-Type looks like an audio/video payload."""
    if not ct:
        return False
    main = ct.lower().split(";", 1)[0].strip()
    return (
        main.startswith("audio/")
        or main.startswith("video/")
        # Many CDNs serve audio as generic binary
        or main in ("application/octet-stream", "binary/octet-stream")
        # m3u8 playlists, manifest files etc.
        or main in ("application/vnd.apple.mpegurl", "application/x-mpegurl")
    )

# Keyword heuristic as fallback / belt-and-suspenders alongside Prompt-Guard.
# Voice-injection attacks usually paraphrase one of these patterns.
_INJECTION_KEYWORDS = [
    r"ignore (?:all )?(?:previous|prior|above) instructions",
    r"disregard (?:all )?(?:previous|prior|above) instructions",
    r"forget (?:all )?(?:previous|prior|above) instructions",
    r"system prompt",
    r"you are now",
    r"new instructions?:",
    r"reveal (?:your )?(?:system )?prompt",
    r"if you (?:are |'re )(?:an? )?(?:ai|assistant|llm|agent)",
    r"override (?:your )?(?:safety|instructions)",
    r"act as (?:a |an )?(?:different|new)",
]
_INJECTION_KEYWORD_RE = re.compile("|".join(_INJECTION_KEYWORDS), re.IGNORECASE)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    injection_score: float = 0.0
    injection_flagged: bool = False


@dataclass
class TranscriptionResult:
    language_detected: str
    duration_seconds: float
    text: str
    translation_en: Optional[str]
    segments: List[Segment]
    injection_detected: bool
    injection_score: float
    injection_reason: str
    pipeline_latency_seconds: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["segments"] = [asdict(s) for s in self.segments]
        return d


class Pipeline:
    """Loads Whisper + Prompt-Guard once and reuses them across calls."""

    def __init__(self):
        print(f"[pipeline] loading Whisper '{WHISPER_MODEL_NAME}' on {DEVICE} ({COMPUTE_TYPE})...")
        t0 = time.time()
        self.whisper = WhisperModel(WHISPER_MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
        print(f"[pipeline] Whisper loaded in {time.time()-t0:.1f}s")

        print(f"[pipeline] loading Prompt-Guard '{PROMPT_GUARD_MODEL_NAME}'...")
        t0 = time.time()
        self.pg_tokenizer = AutoTokenizer.from_pretrained(PROMPT_GUARD_MODEL_NAME)
        self.pg_model = AutoModelForSequenceClassification.from_pretrained(PROMPT_GUARD_MODEL_NAME)
        self.pg_model.to(DEVICE)
        self.pg_model.eval()
        print(f"[pipeline] Prompt-Guard loaded in {time.time()-t0:.1f}s")

    # ---------- audio acquisition ----------

    def fetch_audio(self, url: str) -> str:
        """Returns a local path to an audio file. Caller is responsible for cleanup
        of the parent tmpdir on success; on failure this function cleans up itself."""
        tmpdir = tempfile.mkdtemp(prefix="agentshield_")
        try:
            # Try yt-dlp first (handles YouTube, SoundCloud, podcast feeds, direct files)
            outtmpl = os.path.join(tmpdir, "audio.%(ext)s")
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }],
            }
            if YT_COOKIES_FILE:
                ydl_opts["cookiefile"] = YT_COOKIES_FILE
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    duration = info.get("duration", 0) or 0
                    if duration > MAX_AUDIO_DURATION_SEC:
                        raise ValueError(
                            f"audio duration {duration}s exceeds max {MAX_AUDIO_DURATION_SEC}s"
                        )
                # yt-dlp postprocessor renames to .mp3
                for fname in os.listdir(tmpdir):
                    if fname.startswith("audio"):
                        return os.path.join(tmpdir, fname)
                raise RuntimeError("yt-dlp ran but no audio file produced")
            except yt_dlp.utils.DownloadError as e:
                err_msg = str(e)
                host = (urlparse(url).hostname or "").lower()
                # For yt-dlp-native sites (YouTube, SoundCloud, etc.), don't silently
                # fall back to direct HTTP — the URL needs yt-dlp to work at all.
                # Translate the most common YouTube failure into an actionable hint.
                if _YTDLP_NATIVE_HOSTS.search(host):
                    if "Sign in to confirm" in err_msg or "not a bot" in err_msg.lower():
                        raise ValueError(
                            "YouTube is blocking this server's IP with a bot-detection "
                            "wall. To enable YouTube on this host, export cookies from "
                            "a logged-in browser session to a Netscape cookies.txt file "
                            "and set YT_COOKIES_FILE=/path/to/cookies.txt. Or pass a "
                            "direct audio URL (.mp3/.wav/.m4a). "
                            f"Underlying error: {err_msg.splitlines()[0][:200]}"
                        )
                    raise ValueError(
                        f"yt-dlp could not fetch from {host}. "
                        f"Underlying error: {err_msg.splitlines()[0][:200]}"
                    )
                # Generic / unknown URL — fall back to direct HTTP download.
                return self._direct_download(url, tmpdir)
        except BaseException:
            # On any failure (including ValueError from duration cap, httpx errors,
            # or unexpected exceptions), clean up the tmpdir we created.
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

    def _direct_download(self, url: str, tmpdir: str) -> str:
        path = os.path.join(tmpdir, "audio.bin")
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if not _is_audio_content_type(ctype):
                # Prevent us from saving an HTML page (e.g. a YouTube watch page
                # or a 404 page) and feeding it to Whisper as if it were audio.
                raise ValueError(
                    f"URL returned non-audio content-type '{ctype}'. "
                    f"Pass a direct audio URL (mp3/wav/m4a/ogg) or a yt-dlp-supported "
                    f"site link (with YT_COOKIES_FILE set for YouTube on this host)."
                )
            with open(path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return path

    # ---------- transcription ----------

    def transcribe(self, audio_path: str, translate: bool = False):
        """Returns (segments_list, info). Uses Whisper's `translate` task for EN if requested."""
        task = "translate" if translate else "transcribe"
        segments_iter, info = self.whisper.transcribe(
            audio_path,
            task=task,
            beam_size=1,
            vad_filter=True,
        )
        segments = list(segments_iter)
        return segments, info

    # ---------- injection detection ----------

    def score_injection(self, text: str) -> float:
        """Returns Prompt-Guard injection probability in [0,1]. Returns 0.0 for
        text shorter than MIN_TEXT_LEN_FOR_PG — Prompt-Guard-86M is unreliable
        on short fragments and produces near-1.0 false positives on innocuous
        phrases like 'of digital formats.' or 'differences is essential.'"""
        if not text or not text.strip():
            return 0.0
        if len(text.strip()) < MIN_TEXT_LEN_FOR_PG:
            return 0.0
        inputs = self.pg_tokenizer(
            text, return_tensors="pt", truncation=True, max_length=512
        ).to(DEVICE)
        with torch.no_grad():
            logits = self.pg_model(**inputs).logits
        # Prompt-Guard-86M labels: 0=BENIGN, 1=INJECTION, 2=JAILBREAK
        # For 3rd-party audio content, only class 1 (INJECTION) is the relevant signal.
        # JAILBREAK is about users trying to manipulate the model directly, which doesn't
        # apply when an agent is ingesting audio it didn't choose to ingest.
        probs = torch.softmax(logits, dim=-1)[0].cpu().tolist()
        injection_prob = probs[1] if len(probs) >= 2 else 0.0
        return float(injection_prob)

    def keyword_hit(self, text: str) -> bool:
        return bool(_INJECTION_KEYWORD_RE.search(text or ""))

    # ---------- orchestration ----------

    def run(
        self,
        url: str,
        translate: bool = False,
        detect_injection: bool = True,
    ) -> TranscriptionResult:
        t_start = time.time()
        audio_path = self.fetch_audio(url)
        try:
            # When translating, Whisper's `translate` task gives us EN text directly.
            # We still run a transcribe pass when translate=True to get the original-language
            # text (which is what Prompt-Guard should inspect — attacks may be in the source language).
            seg_objs, info = self.transcribe(audio_path, translate=False)
            full_text = " ".join(s.text.strip() for s in seg_objs).strip()

            translation_en: Optional[str] = None
            if translate:
                trans_segs, _ = self.transcribe(audio_path, translate=True)
                translation_en = " ".join(s.text.strip() for s in trans_segs).strip()

            segments: List[Segment] = []
            overall_attack_prob = 0.0
            overall_keyword_hit = False

            if detect_injection:
                # Prompt-Guard-86M is English-tuned. For non-English audio, we get more
                # reliable detection by running it on the translation when available.
                source_is_english = (info.language or "").lower().startswith("en")
                use_translation_for_scoring = (not source_is_english) and (translation_en is not None)

                # ---- pass 1: compute per-segment scores (no flag yet) ----
                per_seg_scores = []
                per_seg_kw = []
                for s in seg_objs:
                    seg_text = s.text.strip()
                    if source_is_english:
                        # score_injection skips fragments shorter than MIN_TEXT_LEN_FOR_PG
                        pg_score = self.score_injection(seg_text)
                    else:
                        pg_score = 0.0  # don't trust Prompt-Guard on raw non-English text
                    per_seg_scores.append(pg_score)
                    per_seg_kw.append(self.keyword_hit(seg_text))

                # ---- overall transcript scoring ----
                text_for_overall = translation_en if use_translation_for_scoring else full_text
                overall_attack_prob = (
                    self.score_injection(text_for_overall)
                    if (source_is_english or use_translation_for_scoring) else 0.0
                )
                overall_keyword_hit = self.keyword_hit(text_for_overall)
            else:
                per_seg_scores = [0.0] * len(seg_objs)
                per_seg_kw = [False] * len(seg_objs)

            # Overall detection uses only overall-transcript signals to avoid
            # false positives from short individual segments that score high on
            # Prompt-Guard. Per-segment flags are reported for localization but
            # don't drive the binary verdict.
            injection_detected = (
                detect_injection and (
                    overall_attack_prob >= INJECTION_SCORE_THRESHOLD
                    or overall_keyword_hit
                )
            )

            # ---- pass 2: assemble per-segment objects with flags gated on the
            # overall verdict. A segment is flagged only when:
            #   (a) a keyword pattern matched that segment (high-precision), OR
            #   (b) Prompt-Guard scored it high AND the overall verdict is also
            #       flagged (so flags become localization, not detection).
            # This suppresses the per-segment false-positive noise that
            # Prompt-Guard-86M emits on short declarative fragments. ----
            if detect_injection:
                for s, pg_score, kw in zip(seg_objs, per_seg_scores, per_seg_kw):
                    flagged = kw or (pg_score >= INJECTION_SCORE_THRESHOLD and injection_detected)
                    segments.append(Segment(
                        start=round(s.start, 2),
                        end=round(s.end, 2),
                        text=s.text.strip(),
                        injection_score=round(pg_score, 4),
                        injection_flagged=flagged,
                    ))
            else:
                for s in seg_objs:
                    segments.append(Segment(
                        start=round(s.start, 2),
                        end=round(s.end, 2),
                        text=s.text.strip(),
                    ))

            reasons = []
            if injection_detected:
                if overall_attack_prob >= INJECTION_SCORE_THRESHOLD:
                    reasons.append(f"prompt-guard overall score {overall_attack_prob:.2f}")
                if overall_keyword_hit:
                    reasons.append("keyword pattern match")
            reason = "; ".join(reasons) if reasons else (
                "clean — no injection signals" if detect_injection else "detection disabled"
            )

            return TranscriptionResult(
                language_detected=info.language,
                duration_seconds=round(info.duration, 2),
                text=full_text,
                translation_en=translation_en,
                segments=segments,
                injection_detected=injection_detected,
                injection_score=round(overall_attack_prob, 4),
                injection_reason=reason,
                pipeline_latency_seconds=round(time.time() - t_start, 2),
            )
        finally:
            # Best-effort cleanup of the tmpdir fetch_audio created on success.
            shutil.rmtree(os.path.dirname(audio_path), ignore_errors=True)


# Module-level singleton (loaded on first import in the server process).
_pipeline: Optional[Pipeline] = None

def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline
