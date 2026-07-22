import json
import os
import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import server


class ServerTests(unittest.TestCase):
    def make_settings(self, **overrides):
        values = dict(
            api_key="test-key",
            model="gpt-realtime-2.1",
            cognitive_voice="cedar",
            neutral_voice="marin",
            affective_voice="coral",
            transcription_model="gpt-4o-mini-transcribe",
            input_language="en",
            safety_identifier="local-demo-user",
            host="127.0.0.1",
            port=8000,
            tls_cert_file=None,
            tls_key_file=None,
        )
        values.update(overrides)
        return server.Settings(**values)

    def test_placeholder_key_is_not_configured(self):
        settings = self.make_settings(api_key="YOUR_OPENAI_API_KEY_HERE")
        self.assertFalse(settings.api_key_is_configured)

    def test_session_config_contains_neutral_realtime_audio_settings(self):
        payload = json.loads(server.build_session_config(self.make_settings(), "neutral"))
        self.assertEqual(payload["type"], "realtime")
        self.assertEqual(payload["model"], "gpt-realtime-2.1")
        self.assertEqual(payload["output_modalities"], ["audio"])
        self.assertEqual(payload["audio"]["output"]["voice"], "marin")
        self.assertEqual(payload["audio"]["output"]["speed"], 1.0)
        self.assertEqual(payload["audio"]["input"]["transcription"]["language"], "en")
        self.assertIn("20 to 30 English words", payload["instructions"])
        self.assertEqual(
            payload["audio"]["input"]["turn_detection"]["type"], "semantic_vad"
        )

    def test_each_assistant_mode_has_its_voice_speed_and_opening(self):
        expected = {
            "cognitive": ("Logan", "cedar", 1.08),
            "neutral": ("Nomi", "marin", 1.0),
            "affective": ("Sunny", "coral", 0.93),
        }
        for mode_name, (assistant_name, voice, speed) in expected.items():
            with self.subTest(mode=mode_name):
                mode = server.ASSISTANT_MODES[mode_name]
                payload = json.loads(
                    server.build_session_config(self.make_settings(), mode_name)
                )
                self.assertEqual(mode["assistant_name"], assistant_name)
                self.assertEqual(payload["audio"]["output"]["voice"], voice)
                self.assertEqual(payload["audio"]["output"]["speed"], speed)
                self.assertGreaterEqual(len(mode["opening"].split()), 20)
                self.assertLessEqual(len(mode["opening"].split()), 30)

    def test_invalid_assistant_mode_is_rejected(self):
        self.assertFalse(server.assistant_mode_is_valid("unsupported"))
        with self.assertRaisesRegex(ValueError, "Unsupported assistant mode"):
            server.build_session_config(self.make_settings(), "unsupported")

    def test_multipart_contains_both_fields_and_closing_boundary(self):
        body = server.encode_multipart(
            {"sdp": "v=0\r\n", "session": "{}"}, "test-boundary"
        )
        self.assertIn(b'name="sdp"', body)
        self.assertIn(b'name="session"', body)
        self.assertTrue(body.endswith(b"--test-boundary--\r\n"))

    def test_env_file_does_not_override_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("EXAMPLE_SETTING=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"EXAMPLE_SETTING": "from-shell"}, clear=False):
                server.load_env_file(path)
                self.assertEqual(os.environ["EXAMPLE_SETTING"], "from-shell")

    def test_participant_id_validation(self):
        for participant_id in ("P001", "study_12", "participant-9"):
            with self.subTest(participant_id=participant_id):
                self.assertTrue(server.participant_id_is_valid(participant_id))
        for participant_id in ("", "has space", "../escape", "a" * 65):
            with self.subTest(participant_id=participant_id):
                self.assertFalse(server.participant_id_is_valid(participant_id))

    def test_recording_extensions(self):
        self.assertEqual(server.recording_extension("audio/webm;codecs=opus"), ".webm")
        self.assertEqual(server.recording_extension("audio/mp4"), ".m4a")
        self.assertEqual(server.recording_extension("audio/ogg; codecs=opus"), ".ogg")
        self.assertIsNone(server.recording_extension("application/octet-stream"))

    def test_recording_stem_validation(self):
        for recording_stem in ("260721_P001", "260721_study_12_02"):
            with self.subTest(recording_stem=recording_stem):
                self.assertTrue(server.recording_stem_is_valid(recording_stem))
        for recording_stem in ("", "260721 has-space", "../../escape", "20260721_P1"):
            with self.subTest(recording_stem=recording_stem):
                self.assertFalse(server.recording_stem_is_valid(recording_stem))

    def test_recording_path_uses_date_participant_and_collision_suffix(self):
        recorded_at = datetime(2026, 7, 21, 10, 30)
        with tempfile.TemporaryDirectory() as directory:
            recording_directory = Path(directory)
            first = server.next_recording_path(
                recording_directory, "P001", ".webm", recorded_at
            )
            self.assertEqual(first.name, "260721_P001.webm")
            first.touch()
            second = server.next_recording_path(
                recording_directory, "P001", ".webm", recorded_at
            )
            self.assertEqual(second.name, "260721_P001_02.webm")

    def test_recording_path_avoids_other_audio_formats_and_existing_text_logs(self):
        recorded_at = datetime(2026, 7, 21, 10, 30)
        with tempfile.TemporaryDirectory() as audio_directory:
            with tempfile.TemporaryDirectory() as text_directory:
                audio_path = Path(audio_directory)
                text_path = Path(text_directory)
                (audio_path / "260721_P002.m4a").touch()
                (text_path / "260721_P002_02.txt").touch()
                candidate = server.next_recording_path(
                    audio_path,
                    "P002",
                    ".webm",
                    recorded_at,
                    text_path,
                )
                self.assertEqual(candidate.name, "260721_P002_03.webm")

    def test_store_recording_streams_bytes_and_uses_final_filename(self):
        recorded_at = datetime(2026, 7, 21, 10, 30)
        audio = b"test-audio-bytes"
        with tempfile.TemporaryDirectory() as directory:
            path = server.store_recording(
                BytesIO(audio),
                len(audio),
                Path(directory),
                "P003",
                ".webm",
                recorded_at,
            )
            self.assertEqual(path.name, "260721_P003.webm")
            self.assertEqual(path.read_bytes(), audio)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_store_recording_rejects_an_incomplete_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OSError, "disconnected"):
                server.store_recording(
                    BytesIO(b"short"),
                    10,
                    Path(directory),
                    "P004",
                    ".webm",
                )
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_store_text_log_uses_recording_stem_and_utf8(self):
        transcript = "Participant: Hello.\nNomi: Hello, how can I help?\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = server.store_text_log(
                BytesIO(transcript),
                len(transcript),
                Path(directory),
                "260721_P005_02",
            )
            self.assertEqual(path.name, "260721_P005_02.txt")
            self.assertEqual(path.read_bytes(), transcript)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_store_text_log_is_idempotent_but_does_not_overwrite(self):
        first = b"Nomi: First version.\n"
        second = b"Nomi: Different version.\n"
        with tempfile.TemporaryDirectory() as directory:
            transcript_directory = Path(directory)
            server.store_text_log(
                BytesIO(first), len(first), transcript_directory, "260721_P006"
            )
            same_path = server.store_text_log(
                BytesIO(first), len(first), transcript_directory, "260721_P006"
            )
            self.assertEqual(same_path.read_bytes(), first)
            with self.assertRaisesRegex(FileExistsError, "different text log"):
                server.store_text_log(
                    BytesIO(second),
                    len(second),
                    transcript_directory,
                    "260721_P006",
                )
            self.assertEqual(same_path.read_bytes(), first)

    def test_store_text_log_rejects_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UnicodeDecodeError):
                server.store_text_log(
                    BytesIO(b"\xff\xfe"), 2, Path(directory), "260721_P007"
                )
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
