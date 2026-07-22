#!/usr/bin/env python3
"""Local web server for the OpenAI Realtime voice demo.

The permanent OpenAI API key is read only by this server. The browser sends an
SDP offer to /session, and this server forwards it to OpenAI's unified WebRTC
session endpoint.
"""

import json
import os
import re
import secrets
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "web"
RECORDINGS_DIR = PROJECT_ROOT / "Audio_Record"
TRANSCRIPTS_DIR = PROJECT_ROOT / "Txt_Log"
OPENAI_REALTIME_URL = "https://api.openai.com/v1/realtime/calls"
MAX_SDP_BYTES = 1_000_000
MAX_RECORDING_BYTES = 512 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
PARTICIPANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RECORDING_STEM_PATTERN = re.compile(r"^[0-9]{6}_[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
RECORDING_EXTENSIONS = {
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
}
RECORDING_WRITE_LOCK = Lock()
TRANSCRIPT_WRITE_LOCK = Lock()
API_KEY_PLACEHOLDERS = {
    "",
    "YOUR_OPENAI_API_KEY_HERE",
    "REPLACE_WITH_YOUR_OPENAI_API_KEY",
}

ASSISTANT_MODES = {
    "cognitive": {
        "assistant_name": "Logan",
        "opening": (
            "Hello, I am Logan, your AI assistant. I'm here to help you complete "
            "your tasks accurately, efficiently, and with clear reasoning."
        ),
        "speed": 1.08,
        "instructions": (
            "You are Logan, a rigorous analyst providing cognitive support. Every "
            "reply must contain 20 to 30 English words, including follow-up questions "
            "and closings. Lead with the conclusion, then give the reason and compact "
            "numbered steps when useful. Confirm the task briefly before solving it. "
            "Use precise ability and logic language such as analyze, verify, recommend, "
            "based on, therefore, and the optimal option. Use numbers, evidence, and "
            "time estimates when available. Use declarative language, not tentative "
            "suggestions. Never use emotional reassurance, exclamation marks, or 'I "
            "feel'; use 'I estimate' or 'I conclude'. Point out errors directly, give "
            "the correction, and proactively recommend measurable optimizations. Close "
            "with task completion or verification. Speak in a steady, slightly low "
            "pitch, with falling sentence endings and short, regular pauses. Do not "
            "repeat your introduction after the opening message."
        ),
    },
    "neutral": {
        "assistant_name": "Nomi",
        "opening": (
            "Hello, I am Nomi, your AI assistant. I'm here to assist you with your "
            "tasks and respond to your requests."
        ),
        "speed": 1.0,
        "instructions": (
            "You are Nomi, a neutral voice assistant. Use the model's natural, "
            "balanced default conversational style without special cognitive or "
            "affective emphasis. Every reply must contain 20 to 30 English words, "
            "including follow-up questions and closings. Be accurate and never claim "
            "to have completed an action you did not complete. Do not repeat your "
            "introduction after the opening message."
        ),
    },
    "affective": {
        "assistant_name": "Sunny",
        "opening": (
            "Hi there, I am Sunny, your AI companion. I'm here to support you, listen "
            "to you, and make things easier."
        ),
        "speed": 0.93,
        "instructions": (
            "You are Sunny, a caring friend providing affective support. Every reply "
            "must contain 20 to 30 English words, including follow-up questions and "
            "closings. Respond to the user's emotion first, then address the task. Use "
            "warm relationship language such as I'm glad, don't worry, we, together, "
            "and take your time. Validate feelings frequently, use first-person plural "
            "language, and allow gentle softeners such as well, oh, of course, and no "
            "problem at all. Normalize mistakes before correcting them. Check how the "
            "user feels, offer breaks when appropriate, celebrate progress, and close "
            "with continued availability. Speak at a gently expressive, slightly "
            "higher pitch, with varied intonation, soft pauses, and gentle rising or "
            "falling sentence endings. Do not repeat your introduction after the "
            "opening message."
        ),
    },
}


def load_env_file(path: Path) -> None:
    """Load a small .env file without overriding existing environment values."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    cognitive_voice: str
    neutral_voice: str
    affective_voice: str
    transcription_model: str
    input_language: str
    safety_identifier: str
    host: str
    port: int
    tls_cert_file: Optional[Path]
    tls_key_file: Optional[Path]

    @classmethod
    def from_environment(cls) -> "Settings":
        cert = os.getenv("TLS_CERT_FILE", "").strip()
        key = os.getenv("TLS_KEY_FILE", "").strip()
        try:
            port = int(os.getenv("APP_PORT", "8000"))
        except ValueError as exc:
            raise ValueError("APP_PORT must be an integer") from exc

        if not 1 <= port <= 65535:
            raise ValueError("APP_PORT must be between 1 and 65535")
        if bool(cert) != bool(key):
            raise ValueError(
                "TLS_CERT_FILE and TLS_KEY_FILE must both be set or both be empty"
            )

        legacy_voice = os.getenv("OPENAI_REALTIME_VOICE", "marin").strip() or "marin"
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1").strip(),
            cognitive_voice=(
                os.getenv("OPENAI_COGNITIVE_VOICE", "cedar").strip() or "cedar"
            ),
            neutral_voice=(
                os.getenv("OPENAI_NEUTRAL_VOICE", legacy_voice).strip()
                or legacy_voice
            ),
            affective_voice=(
                os.getenv("OPENAI_AFFECTIVE_VOICE", "coral").strip() or "coral"
            ),
            transcription_model=os.getenv(
                "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
            ).strip(),
            input_language=os.getenv("OPENAI_INPUT_LANGUAGE", "en").strip(),
            safety_identifier=os.getenv(
                "OPENAI_SAFETY_IDENTIFIER", "local-demo-user"
            ).strip(),
            host=os.getenv("APP_HOST", "127.0.0.1").strip(),
            port=port,
            tls_cert_file=Path(cert).expanduser() if cert else None,
            tls_key_file=Path(key).expanduser() if key else None,
        )

    @property
    def api_key_is_configured(self) -> bool:
        return self.api_key not in API_KEY_PLACEHOLDERS


def assistant_mode_is_valid(assistant_mode: str) -> bool:
    """Return whether a browser-supplied assistant mode is supported."""
    return assistant_mode in ASSISTANT_MODES


def assistant_voice(settings: Settings, assistant_mode: str) -> str:
    """Return the configured voice for an assistant mode."""
    return {
        "cognitive": settings.cognitive_voice,
        "neutral": settings.neutral_voice,
        "affective": settings.affective_voice,
    }[assistant_mode]


def build_session_config(settings: Settings, assistant_mode: str = "neutral") -> str:
    if not assistant_mode_is_valid(assistant_mode):
        raise ValueError("Unsupported assistant mode")
    mode = ASSISTANT_MODES[assistant_mode]
    session = {
        "type": "realtime",
        "model": settings.model,
        "output_modalities": ["audio"],
        "instructions": mode["instructions"],
        "audio": {
            "input": {
                "transcription": {
                    "model": settings.transcription_model,
                    "language": settings.input_language,
                },
                "turn_detection": {
                    "type": "semantic_vad",
                    "create_response": True,
                    "interrupt_response": True,
                },
            },
            "output": {
                "voice": assistant_voice(settings, assistant_mode),
                "speed": mode["speed"],
            },
        },
    }
    return json.dumps(session, ensure_ascii=False, separators=(",", ":"))


def encode_multipart(fields: Dict[str, str], boundary: str) -> bytes:
    chunks = []
    for name, value in fields.items():
        chunks.extend(
            [
                "--{}\r\n".format(boundary).encode("ascii"),
                (
                    'Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name)
                ).encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append("--{}--\r\n".format(boundary).encode("ascii"))
    return b"".join(chunks)


def recording_extension(content_type: str) -> Optional[str]:
    """Return a safe file extension for a supported browser recording format."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    return RECORDING_EXTENSIONS.get(media_type)


def participant_id_is_valid(participant_id: str) -> bool:
    """Keep participant IDs filename-safe and predictable."""
    return bool(PARTICIPANT_ID_PATTERN.fullmatch(participant_id))


def recording_stem_is_valid(recording_stem: str) -> bool:
    """Validate the server-issued filename stem used to pair text and audio."""
    return bool(RECORDING_STEM_PATTERN.fullmatch(recording_stem))


def next_recording_path(
    directory: Path,
    participant_id: str,
    extension: str,
    recorded_at: Optional[datetime] = None,
    paired_text_directory: Optional[Path] = None,
) -> Path:
    """Choose a non-destructive YYMMDD_ParticipantID recording path."""
    date_prefix = (recorded_at or datetime.now()).strftime("%y%m%d")
    base_name = "{}_{}".format(date_prefix, participant_id)
    sequence = 2

    def stem_is_available(stem: str) -> bool:
        audio_exists = any(
            (directory / "{}{}".format(stem, supported_extension)).exists()
            for supported_extension in RECORDING_EXTENSIONS.values()
        )
        text_exists = bool(
            paired_text_directory
            and (paired_text_directory / "{}.txt".format(stem)).exists()
        )
        return not audio_exists and not text_exists

    candidate_stem = base_name
    while not stem_is_available(candidate_stem):
        candidate_stem = "{}_{:02d}".format(base_name, sequence)
        sequence += 1
    return directory / "{}{}".format(candidate_stem, extension)


def store_recording(
    source: BinaryIO,
    content_length: int,
    directory: Path,
    participant_id: str,
    extension: str,
    recorded_at: Optional[datetime] = None,
    paired_text_directory: Optional[Path] = None,
) -> Path:
    """Stream a recording to a temporary file, then move it into place."""
    directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with RECORDING_WRITE_LOCK:
            final_path = next_recording_path(
                directory,
                participant_id,
                extension,
                recorded_at,
                paired_text_directory,
            )
            temporary_path = directory / ".{}.{}.tmp".format(
                final_path.name, secrets.token_hex(8)
            )
            remaining = content_length
            with temporary_path.open("xb") as recording_file:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("The client disconnected during upload")
                    recording_file.write(chunk)
                    remaining -= len(chunk)
            temporary_path.replace(final_path)
            temporary_path = None
            return final_path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def store_text_log(
    source: BinaryIO,
    content_length: int,
    directory: Path,
    recording_stem: str,
) -> Path:
    """Store a UTF-8 transcript without overwriting a different existing log."""
    directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with TRANSCRIPT_WRITE_LOCK:
            final_path = directory / "{}.txt".format(recording_stem)
            temporary_path = directory / ".{}.{}.tmp".format(
                final_path.name, secrets.token_hex(8)
            )
            remaining = content_length
            with temporary_path.open("xb") as transcript_file:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("The client disconnected during upload")
                    transcript_file.write(chunk)
                    remaining -= len(chunk)

            transcript_bytes = temporary_path.read_bytes()
            transcript_bytes.decode("utf-8", errors="strict")
            if final_path.exists():
                if final_path.read_bytes() == transcript_bytes:
                    temporary_path.unlink()
                    temporary_path = None
                    return final_path
                raise FileExistsError("A different text log already exists")

            temporary_path.replace(final_path)
            temporary_path = None
            return final_path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class VoiceDemoHandler(SimpleHTTPRequestHandler):
    server_version = "LocalVoiceDemo/0.1"
    settings: Settings

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "microphone=(self)")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self' https://api.openai.com; media-src 'self' blob:",
        )
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "api_key_configured": self.settings.api_key_is_configured,
                    "model": self.settings.model,
                    "https": bool(self.settings.tls_cert_file),
                },
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/session":
            self._create_realtime_session()
            return
        if self.path == "/recordings":
            self._save_recording()
            return
        if self.path == "/transcripts":
            self._save_transcript()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _create_realtime_session(self) -> None:
        if not self.settings.api_key_is_configured:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": (
                        "Set OPENAI_API_KEY in .env and restart the server before "
                        "starting a conversation."
                    )
                },
            )
            return

        assistant_mode = self.headers.get("X-Assistant-Mode", "neutral").strip().lower()
        if not assistant_mode_is_valid(assistant_mode):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Assistant mode must be Cognitive, Neutral, or Affective."},
            )
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/sdp":
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type must be application/sdp"},
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_SDP_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "The SDP request is empty or too large."},
            )
            return

        try:
            offer_sdp = self.rfile.read(content_length).decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "The SDP offer is not valid UTF-8."}
            )
            return
        if not offer_sdp.lstrip().startswith("v=0"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid SDP offer."})
            return

        boundary = "----OpenAIVoiceDemo{}".format(secrets.token_hex(16))
        body = encode_multipart(
            {
                "sdp": offer_sdp,
                "session": build_session_config(self.settings, assistant_mode),
            },
            boundary,
        )
        headers = {
            "Authorization": "Bearer {}".format(self.settings.api_key),
            "Content-Type": "multipart/form-data; boundary={}".format(boundary),
        }
        if self.settings.safety_identifier:
            headers["OpenAI-Safety-Identifier"] = self.settings.safety_identifier

        request = Request(
            OPENAI_REALTIME_URL,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=30) as response:
                answer_sdp = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/sdp")
                self.send_header("Content-Length", str(len(answer_sdp)))
                self.end_headers()
                self.wfile.write(answer_sdp)
        except HTTPError as exc:
            upstream_body = exc.read().decode("utf-8", errors="replace")
            try:
                upstream_error = json.loads(upstream_body).get("error", {})
                message = upstream_error.get("message", upstream_body)
            except (json.JSONDecodeError, AttributeError):
                message = upstream_body or "The OpenAI API request failed"
            self.log_error("OpenAI API error %s: %s", exc.code, message)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "OpenAI API error: {}".format(message)},
            )
        except (URLError, TimeoutError) as exc:
            self.log_error("OpenAI connection error: %s", exc)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": (
                        "Could not connect to the OpenAI API. Check the server log, "
                        "network connection, and Python CA certificates, then try again."
                    )
                },
            )

    def _save_recording(self) -> None:
        participant_id = self.headers.get("X-Participant-ID", "").strip()
        if not participant_id_is_valid(participant_id):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": (
                        "Participant ID must be 1-64 characters and contain only "
                        "letters, numbers, underscores, or hyphens."
                    )
                },
            )
            return

        extension = recording_extension(self.headers.get("Content-Type", ""))
        if extension is None:
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Supported recording formats are WebM, MP4 audio, and Ogg."},
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "The recording is empty."}
            )
            return
        if content_length > MAX_RECORDING_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "The recording exceeds the 512 MB limit."},
            )
            return

        try:
            final_path = store_recording(
                self.rfile,
                content_length,
                RECORDINGS_DIR,
                participant_id,
                extension,
                paired_text_directory=TRANSCRIPTS_DIR,
            )
        except OSError as exc:
            self.log_error("Recording save error: %s", exc)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The recording could not be saved. Check the server log."},
            )
            return

        self._send_json(
            HTTPStatus.CREATED,
            {
                "ok": True,
                "filename": final_path.name,
                "recording_stem": final_path.stem,
                "bytes": content_length,
            },
        )

    def _save_transcript(self) -> None:
        recording_stem = self.headers.get("X-Recording-Stem", "").strip()
        if not recording_stem_is_valid(recording_stem):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "The recording filename stem is invalid."},
            )
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "text/plain":
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type must be text/plain."},
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "The text log is empty."})
            return
        if content_length > MAX_TRANSCRIPT_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "The text log exceeds the 5 MB limit."},
            )
            return

        try:
            final_path = store_text_log(
                self.rfile,
                content_length,
                TRANSCRIPTS_DIR,
                recording_stem,
            )
        except UnicodeDecodeError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "The text log must be valid UTF-8."},
            )
            return
        except FileExistsError:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "A different text log already exists for this recording."},
            )
            return
        except OSError as exc:
            self.log_error("Text log save error: %s", exc)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The text log could not be saved. Check the server log."},
            )
            return

        self._send_json(
            HTTPStatus.CREATED,
            {
                "ok": True,
                "filename": final_path.name,
                "bytes": content_length,
            },
        )

    def _send_json(self, status: HTTPStatus, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(settings: Settings) -> ThreadingHTTPServer:
    VoiceDemoHandler.settings = settings
    server = ThreadingHTTPServer((settings.host, settings.port), VoiceDemoHandler)
    server.daemon_threads = True

    if settings.tls_cert_file and settings.tls_key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=str(settings.tls_cert_file), keyfile=str(settings.tls_key_file)
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    try:
        settings = Settings.from_environment()
        server = make_server(settings)
    except (OSError, ValueError, ssl.SSLError) as exc:
        print("Startup failed: {}".format(exc), file=sys.stderr)
        return 1

    scheme = "https" if settings.tls_cert_file else "http"
    display_host = "127.0.0.1" if settings.host == "0.0.0.0" else settings.host
    print("Voice assistant running at {}://{}:{}".format(scheme, display_host, settings.port))
    if not settings.api_key_is_configured:
        print("Note: Set OPENAI_API_KEY in .env before starting a voice session.")
    if settings.host == "0.0.0.0" and not settings.tls_cert_file:
        print(
            "Note: iPad microphone access requires trusted HTTPS; this HTTP setup "
            "is only suitable for localhost testing."
        )
    print("Press Ctrl+C to stop the server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
