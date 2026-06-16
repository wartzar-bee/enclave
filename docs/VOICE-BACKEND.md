# Voice backend (optional, privacy opt-in)

The chat's voice features work **in the browser by default** (zero infra): dictation via the Web
Speech API, spoken replies via `speechSynthesis`. That's portable, but in Chrome dictation audio is
sent to the browser vendor's cloud STT. For security-sensitive deployments you can route voice
through a **server-side service you control** (e.g. a local whisper/`mlx-whisper` transcriber and an
`edge-tts`/VibeVoice speaker) by pointing two env vars at it.

When `TRANSCRIBE_URL` / `TTS_URL` are set, `web_chat.py` proxies to them and the UI uses them; when
unset, the UI falls back to the browser. The agent container never holds these creds — the chat
sidecar proxies host-side, same pattern as the other bridges.

## Configure
In `.env` (passed through `docker-compose.yml` to the web-chat sidecar):
```
TRANSCRIBE_URL=http://host.docker.internal:18185/transcribe
TTS_URL=http://host.docker.internal:18186/tts
TTS_VOICE=                # optional default voice id, passed through to the TTS service
```
`host.docker.internal` reaches a service on the Docker host (Linux uses the `extra_hosts` mapping).

## Contracts (what the service must implement)
**STT** — `POST {TRANSCRIBE_URL}`
```jsonc
// request  (JSON)
{ "audio_base64": "<base64 of the recorded audio, no data: prefix>", "mime": "audio/webm" }
// response (JSON)
{ "text": "the transcript" }
```

**TTS** — `POST {TTS_URL}`
```jsonc
// request  (JSON)
{ "text": "what to speak", "voice": "<optional voice id>" }
// response: raw audio bytes with an audio/* Content-Type (e.g. audio/mpeg)
```

Any service matching these two contracts works — write a thin adapter in front of whatever
STT/TTS engine you run. Keep it bound to the host/loopback; the chat sidecar is the only caller.
