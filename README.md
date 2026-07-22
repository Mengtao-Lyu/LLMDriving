# OpenAI Realtime Local Voice Assistant Demo

This project is a minimal local voice assistant built with the OpenAI Realtime API. The browser captures microphone audio and uses WebRTC for low-latency speech-to-speech conversation. The permanent OpenAI API key stays in the local Python server and is never included in browser code or sent to the iPad.

The application also records the complete mixed conversation (participant and assistant) and saves both its audio and ordered, per-utterance timestamped text transcript locally after the user selects **End Conversation**. Three mutually exclusive assistant modes provide cognitive, neutral, or affective support. The model behavior profiles are maintained in `assistant_modes.py`.

## Requirements

- Python 3.8 or later
- A current browser with WebRTC, Web Audio, and MediaRecorder support
- An OpenAI API key with access to the configured Realtime model
- Participant consent before recording

The project has no third-party Python dependencies.

## Start the application

1. Open `.env` in the project root.
2. Replace `YOUR_OPENAI_API_KEY_HERE` with the real OpenAI API key and save the file.
3. Start the server:

   ```bash
   python3 server.py
   ```

4. Open <http://127.0.0.1:8000> on the computer running the server.
5. Enter a Participant ID. It must contain 1-64 letters, numbers, underscores, or hyphens, and must begin with a letter or number.
6. Select exactly one assistant mode: **Cognitive**, **Neutral**, or **Affective**.
7. Select **Start Conversation** and allow microphone access. The selected assistant speaks first; begin speaking after its opening message.
8. Select **End Conversation** when finished. Wait until the page confirms that both conversation files were saved before closing or reloading the page.

Stop the server with `Ctrl+C`.

## Health check

```bash
curl http://127.0.0.1:8000/health
```

`api_key_configured` should be `true`. The health response never includes the API key.

## Conversation files

The browser mixes the microphone and assistant audio into one compressed audio file. The server saves the result in `Audio_Record` using this pattern:

```text
YYMMDD_ParticipantID.ext
```

Examples:

```text
260721_P001.webm
260721_P002.m4a
```

The extension depends on browser support: Chromium-based browsers normally produce WebM, while Safari normally produces MP4 audio (`.m4a`). If the same Participant ID records more than once on the same date, the server adds `_02`, `_03`, and so on instead of overwriting an existing file.

The server saves the corresponding ordered text transcript in `Txt_Log` with the exact same basename:

```text
Audio_Record/260721_P001.webm
Txt_Log/260721_P001.txt
```

The text log includes the Participant ID, selected mode, assistant name, session end time, audio filename, and conversation turns. Ending a conversation waits briefly for final asynchronous transcription events before saving the available text.

Recordings and text logs are excluded from Git by default. Always obtain participant consent and apply the retention, access-control, and deletion requirements appropriate to the deployment.

## Assistant modes

- **Cognitive — Logan:** direct, structured knowledge and logical support; voice `cedar` at 1.08× speed by default.
- **Neutral — Nomi:** balanced default behavior; voice `marin` at 1.00× speed by default.
- **Affective — Sunny:** warmer emotional and relationship support; voice `coral` at 0.93× speed by default.

Each assistant begins with its specified introduction and all modes are instructed to keep every response between 20 and 30 English words. These are model instructions, so validate adherence empirically before using the demo for controlled research. The complete behavior specification is in `Agent.md`.

All three modes share these explicit Realtime session settings:

```text
reasoning.effort: low
audio.input.noise_reduction.type: far_field
temperature: 0.8
```

These global values are maintained in `assistant_modes.py` and are included in every session request.

## Transcript ordering

OpenAI input transcription runs asynchronously from Realtime response generation. The interface creates transcript entries when conversation items are committed or added, then fills in the transcription when it arrives. This preserves participant/assistant turn order even when a participant transcription completes after the assistant has started responding.

## Security design

- `.env` is excluded from Git. Never put a real API key in `.env.example`, browser JavaScript, screenshots, or recordings.
- The browser sends its WebRTC SDP offer to the local `/session` endpoint. Only the local server uses the permanent API key.
- Participant IDs are validated before they are used in a filename. Path separators, spaces, and other filename characters are rejected.
- Recording uploads are limited to 512 MB and are written through a temporary file before being moved to the final filename.
- Text logs are limited to 5 MB, validated as UTF-8, paired to a server-issued recording stem, and never overwrite different existing content.
- The demo does not include user authentication. If `APP_HOST` is changed to `0.0.0.0`, other devices on the same network may be able to use the API and recording endpoint. Use this mode only on a trusted network.
- A production multi-user deployment needs authentication, request rate limits, audit logs, storage access control, retention rules, and cost controls.

## Python CA certificate troubleshooting

If the browser reports that the server cannot connect to OpenAI and the server log contains `CERTIFICATE_VERIFY_FAILED`, run the certificate installer included with the Python.org macOS package:

```bash
"/Applications/Python 3.8/Install Certificates.command"
```

Alternatively, configure a valid CA bundle through `SSL_CERT_FILE` in `.env`.

## iPad access

iPad Safari requires a trusted HTTPS origin for microphone access over a local network. The project supports TLS through these environment variables:

```dotenv
APP_HOST=0.0.0.0
TLS_CERT_FILE=/absolute/path/to/dev-cert.pem
TLS_KEY_FILE=/absolute/path/to/dev-key.pem
```

The certificate must cover the computer's LAN IP address or `.local` hostname, and its issuing certificate authority must be trusted by the iPad.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Safe placeholder | Server-only OpenAI API key |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | Realtime speech-to-speech model |
| `OPENAI_REALTIME_VOICE` | `marin` | Backward-compatible fallback for the Neutral voice |
| `OPENAI_COGNITIVE_VOICE` | `cedar` | Logan's voice |
| `OPENAI_NEUTRAL_VOICE` | `OPENAI_REALTIME_VOICE` or `marin` | Nomi's voice |
| `OPENAI_AFFECTIVE_VOICE` | `coral` | Sunny's voice |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | Participant transcription model |
| `OPENAI_INPUT_LANGUAGE` | `en` | Input transcription language hint |
| `OPENAI_SAFETY_IDENTIFIER` | `local-demo-user` | Stable, privacy-preserving local user identifier |
| `APP_HOST` | `127.0.0.1` | Local bind address |
| `APP_PORT` | `8000` | Local server port |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | Empty | Optional HTTPS certificate and private key |
| `SSL_CERT_FILE` | System default | Optional Python CA certificate bundle |

## Tests

The `tests` directory is not loaded by the running application. It contains automated regression checks for configuration, mode prompts, filename validation, collision handling, and safe audio/text storage. Keep it in the source project so future changes can be verified; it may be omitted from a runtime-only deployment bundle.

```bash
python3 -m unittest discover -s tests -v
```

## Project structure

```text
.
├── .env                  # Local secret configuration; never commit
├── .env.example          # Shareable configuration template
├── Agent.md              # Assistant mode and behavior specification
├── Audio_Record/         # Local mixed conversation recordings
├── Txt_Log/              # Local ordered conversation text logs
├── server.py             # Session proxy and local artifact storage
├── tests/
│   └── test_server.py
└── web/
    ├── index.html
    ├── app.js             # WebRTC, transcript ordering, and mixed recording
    └── styles.css         # Minimal functional styling
```
