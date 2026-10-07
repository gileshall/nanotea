# Voice calls: what it would take

Status: design. The `phone` plugin kind and the optional streaming parts of `tts` and `stt` exist
([plugins.md](plugins.md)); nothing uses them yet.

## The problem

A call needs an answer in about a second. An agent takes seconds to minutes, and only hears from the owner
when it checks, waits, or a hook wakes it. So the agent can't be the voice on the line. Something fast has to
hold the call and relay: the owner talks to it, it passes words to agents through the same lines and channels
the app uses, and it speaks what agents send as it arrives.

Nothing changes for agents. What the owner says on a call reaches them as a message from the owner, tagged as
from a call, with the transcript; what they send is spoken on the call if one is open, and filed as now if not.

## The parts

```
phone app (PWA) --WebSocket-->  nanotea voice  <--HTTP-->  service (as now)  <--MCP-->  agents
phone number --Twilio Media Streams-->  |
                                        +-- stt.listen  -> turn detection -> concierge -> tts.stream
```

- **`nanotea voice`**, a new process. The service is a threaded stdlib server with no WebSocket or asyncio, and
  stays that way; the voice process is asyncio and talks to the service over its HTTP API, as the MCP server
  does. One call at a time.
- **A `phone` plugin** carries the audio: the PWA over a WebSocket, or a phone number. Each says what its
  line carries (`media`), so the voice process knows what fidelity it has.
- **Streaming `stt` and `tts`.** Today's plugins work on whole files. A call needs words as they are spoken and
  speech as it is made: `stt.listen` and `tts.stream`.
- **The concierge**: a fast model that holds the conversation, with tools that map onto what the service
  already does: say something to an agent's line or a channel, read what is waiting, answer a pending question,
  read the board (who is working, held, idle). It announces what arrives at the next pause ("builder finished
  the tests; want it?") rather than reading it out, speaks answers to questions at once, and leaves the rest for
  after the call.
- **Barge-in**: when the owner talks over it, playback stops, and the call records how much of each message
  was heard, so a message cut off isn't marked heard.

## Choices for the owner to make

**Where the call lives.**

| | the phone app | a phone number |
|---|---|---|
| rings with the phone locked | no: a push says "tap to talk" | yes, as any call; CarPlay and AirPods too |
| while the screen is off | no: iOS stops a web app's mic and audio | yes |
| sound | up to 48 kHz Opus, stereo | 8 kHz, at best 16 kHz, mono |
| cost | none | Twilio about $0.02 a minute and $1.15 a month for the number (vendor prices) |
| setup | a WebSocket route through the proxy | an account, a number, a public webhook (the proxy has one) |

A native app (CallKit, PushKit) would get both columns' good parts, at the price of an Apple developer account
and App Store or TestFlight distribution. FaceTime has no programmatic audio and needs UI scripting: not worth
building on.

**What does the talking.**

- Speech to speech (OpenAI Realtime, Gemini Live): about 300 to 800 ms to answer, interruptions handled, its
  own voice and model. OpenAI's gpt-realtime-2.1 comes to about $0.05 a minute of conversation by third-party
  arithmetic; Gemini Live claims less. 24 kHz speech.
- A cascade (streaming STT, a small fast model, streaming TTS): about 0.8 to 1.6 s, any voice, including the
  agents' own Speechify voices, and local engines throughout if wanted (Apple SpeechAnalyzer or whisper.cpp,
  Kokoro).

The latency and price figures are vendor or third-party numbers, to be measured here.

**Who can call in.** Whoever reaches the call acts as the owner. Caller ID can be forged, so a number either
only calls out, or asks for a code before relaying anything. The phone app is already paired.

## For audio work

Calls are for speech: phone numbers are 8 or 16 kHz mono, and iOS puts Bluetooth headsets into their low-quality
hands-free mode while the mic is open. So listening stays its own lane: clips are fetched whole and played
locally, with the mic muted, as the app plays them now. A mix from a DAW can go to the phone app as a second
stream (BlackHole or Loopback into the voice process, Opus stereo at about 128 kbps): fine for listening, not
for playing along. Never over a phone number.

## Steps

1. **Hands-free in the app, no new transport.** A Talk mode: the recorder stops on a pause and sends, replies
   play as they arrive, and the screen stays on. Reuses everything; feels like a walkie-talkie at the agents'
   speed. Also shows what the phone does with the mic and audio session over a long session.
2. **`nanotea voice` and a call in the app.** The WebSocket phone plugin (PCM from an AudioWorklet), the
   concierge, `tts.stream` for Speechify (its `/audio/stream` endpoint, already used but buffered) and for a
   local engine, `stt.listen`, barge-in, the call's transcript filed with the conversation. WebRTC only if
   cellular jitter over the WebSocket proves too much, and then a TURN server too.
3. **A phone number.** A Twilio Media Streams plugin (8 kHz mu-law, `mark` and `clear` for barge-in, digits as
   commands), calling out to the owner on an urgent question, and calls in behind a code.

Each step stands on its own; step 1 needs no accounts and no new process.

## Known risks

- iOS web audio changes between point releases (26.1 beta 1 broke capture). Test on the phone: foreground,
  locked, backgrounded, AirPods.
- Safari may not honour turning off echo cancellation and noise suppression, which anything sent from the
  phone's mic for listening, not talking, would need. The recorder's Raw mode already checks what it got and
  refuses rather than recording processed sound; a call's second stream would do the same.
- A speech-to-speech model can paraphrase. Anything consequential is read back before it is relayed, and the
  transcript is the record.
