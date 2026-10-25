#!/usr/bin/env python3
"""Hello Jarvis — Hands-free Voice Agent.

Listens for "Hello Jarvis" (or "Hey Jarvis", "Jarvis"), engages in conversation,
and executes commands such as sending WhatsApp messages.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import wave
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# Configuration paths
CONFIG_DIR = Path.home() / ".openjarvis"
CONTACTS_FILE = CONFIG_DIR / "contacts.json"

# Audio constants
SAMPLE_RATE = 16000
CHUNK = 1024
SILENCE_THRESHOLD = 500
SILENCE_SECONDS = 1.5
STARTUP_SILENCE_SECONDS = 4.0

def notify_dashboard(is_listening: bool = False, heard: str = "", response: str = ""):
    """Send real-time voice updates to the Jarvis dashboard GUI."""
    try:
        payload = json.dumps({
            "is_listening": is_listening,
            "last_heard": heard,
            "last_response": response,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:5050/api/voice/update",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass

def load_contacts() -> Dict[str, str]:
    """Load friendly contact name -> phone number mappings."""
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text())
        except Exception:
            return {}
    default_contacts = {
        "me": "+918879223352",
        "myself": "+918879223352",
        "mom": "+918879223352",
        "baba": "+918879223352",
        "riya": "+918879223352",
    }
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONTACTS_FILE.write_text(json.dumps(default_contacts, indent=2))
    except Exception:
        pass
    return default_contacts

def save_contact(name: str, number: str) -> None:
    """Save or update a contact."""
    contacts = load_contacts()
    contacts[name.lower().strip()] = number.strip()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONTACTS_FILE.write_text(json.dumps(contacts, indent=2))
        print(f"[Contacts] Saved {name} -> {number}")
    except Exception as e:
        print(f"[Contacts] Error saving contact: {e}")

def record_audio(
    max_seconds: float = 10.0,
    silence_threshold: int = SILENCE_THRESHOLD,
    silence_seconds: float = SILENCE_SECONDS,
    startup_silence: float = STARTUP_SILENCE_SECONDS,
) -> bytes:
    """Record from the default microphone until silence or max duration."""
    import sounddevice as sd

    chunks_per_sec = SAMPLE_RATE / CHUNK
    max_chunks = int(max_seconds * chunks_per_sec)
    silence_chunks = int(silence_seconds * chunks_per_sec)
    startup_chunks = int(startup_silence * chunks_per_sec)

    frames = []
    has_speech = False
    silence_count = 0

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK,
    ) as stream:
        for _ in range(max_chunks):
            data, _ = stream.read(CHUNK)
            raw = bytes(data)
            frames.append(raw)

            # Calculate RMS
            import struct
            n = len(raw) // 2
            shorts = struct.unpack(f"{n}h", raw[: n * 2])
            rms = (sum(s * s for s in shorts) / max(1, n)) ** 0.5

            if rms > silence_threshold:
                has_speech = True
                silence_count = 0
            elif not has_speech and len(frames) >= startup_chunks:
                break
            elif has_speech:
                silence_count += 1
                if silence_count >= silence_chunks:
                    break

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()

class VoiceAgent:
    def __init__(self):
        print("🤖 Initializing Jarvis Voice Agent...")
        self.contacts = load_contacts()
        self._init_speech()
        self._init_whatsapp()
        self._init_llm()

    def _init_speech(self):
        """Initialize Whisper STT and Kokoro/ElevenLabs TTS."""
        from openjarvis.cli._voice_chat import VoiceSession, speak
        self.session = VoiceSession()
        self.stt = self.session.get_stt_backend()
        self.tts = self.session.get_tts_backend()
        self.speak_fn = speak
        print(f"🎤 Speech-to-Text: {getattr(self.stt, 'backend_id', 'whisper')}")
        print(f"🔊 Text-to-Speech: {getattr(self.tts, 'backend_id', 'kokoro')}")

    def _init_whatsapp(self):
        """Initialize WhatsApp Baileys channel."""
        try:
            from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
            self.wa_channel = WhatsAppBaileysChannel()
            print("📱 WhatsApp channel backend loaded.")
        except Exception as e:
            print(f"⚠️  WhatsApp backend warning: {e}")
            self.wa_channel = None

    def _init_llm(self):
        """Check Ollama availability and preload model into memory."""
        import urllib.request
        try:
            req = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
            data = json.loads(req.read().decode())
            models = [m.get("name") for m in data.get("models", [])]
            # Prefer qwen3.5:4b for faster voice response if available, else llama3
            if any("qwen3.5" in m for m in models):
                self.model = next(m for m in models if "qwen3.5" in m)
            elif any("llama3" in m for m in models):
                self.model = next(m for m in models if "llama3" in m)
            else:
                self.model = models[0] if models else "llama3:latest"
            print(f"🧠 Local Brain: Ollama ({self.model})")
            
            # Preload into memory asynchronously or with gentle timeout
            try:
                preload_data = json.dumps({"model": self.model, "keep_alive": "1h"}).encode()
                pre_req = urllib.request.Request("http://localhost:11434/api/generate", data=preload_data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(pre_req, timeout=1)
            except Exception:
                pass
        except Exception:
            self.model = "llama3:latest"
            print("🧠 Local Brain: Ollama (waiting for server on :11434)")

    def speak(self, text: str):
        """Speak response aloud."""
        print(f"\n🗣️  Jarvis: {text}")
        try:
            from rich.console import Console
            self.speak_fn(text, Console(stderr=True), self.session)
        except Exception as e:
            print(f"[TTS Error: {e}]")

    def transcribe(self, audio_bytes: bytes) -> str:
        """Transcribe audio to text."""
        if not audio_bytes or len(audio_bytes) < 4000:
            return ""
        try:
            result = self.stt.transcribe(audio_bytes, format="wav")
            return result.text.strip()
        except Exception as e:
            print(f"[STT Error: {e}]")
            return ""

    def send_whatsapp(self, recipient: str, message: str) -> bool:
        """Send WhatsApp message to recipient."""
        # Check contacts map
        rec_clean = recipient.lower().strip()
        target = self.contacts.get(rec_clean, recipient)

        print(f"📤 Sending WhatsApp to: {recipient} ({target}): {message}")
        try:
            from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
            if self.wa_channel is None:
                self.wa_channel = WhatsAppBaileysChannel()
            return self.wa_channel.send(target, message)
        except Exception as e:
            print(f"[WhatsApp Send Error: {e}]")
            return False

    def handle_command(self, text: str) -> bool:
        """Handle specific command intents like WhatsApp messaging."""
        lower = text.lower()

        # WhatsApp intent matching
        if "whatsapp" in lower or "text" in lower:
            recipient = ""
            msg = ""

            # Check if any contact name appears in the sentence
            for name in self.contacts:
                if name in lower:
                    recipient = name
                    break

            # If no contact name, look for numbers
            if not recipient:
                digits = re.findall(r"\+?\d{8,14}", text)
                if digits:
                    recipient = digits[0]

            # Extract message
            parts = re.split(r"(?:saying|that|message)\s+", text, flags=re.IGNORECASE)
            if len(parts) > 1:
                msg = parts[-1].strip().strip('"\'')
            else:
                msg = text

            if recipient and msg:
                self.speak(f"Sending message to {recipient} on WhatsApp.")
                success = self.send_whatsapp(recipient, msg)
                if success:
                    self.speak(f"The message has been sent to {recipient}.")
                else:
                    self.speak(f"I encountered an issue sending the message to {recipient}.")
                return True
            elif not recipient:
                self.speak("Who would you like me to send the WhatsApp message to?")
                # Record follow up
                followup_audio = record_audio(max_seconds=5.0)
                followup_text = self.transcribe(followup_audio)
                if followup_text:
                    recipient = followup_text.strip().lower()
                    self.speak(f"What should I say to {recipient}?")
                    msg_audio = record_audio(max_seconds=8.0)
                    msg_text = self.transcribe(msg_audio)
                    if msg_text:
                        self.send_whatsapp(recipient, msg_text)
                        self.speak(f"Message sent to {recipient}.")
                        return True
                return True

        return False

    def ask_llm(self, prompt: str) -> str:
        """Generate response using Ollama LLM."""
        import urllib.request

        # qwen3.5 has a "thinking" mode that takes 60+ seconds before answering.
        # For fast voice responses, always use llama3 which has no thinking overhead.
        voice_model = "llama3:latest"

        system_prompt = (
            "You are Jarvis, a sophisticated, helpful, concise AI assistant. "
            "Respond politely and concisely (1-3 sentences max) for voice speech. "
            "Do NOT use markdown, bullet points or special formatting."
        )
        data = {
            "model": voice_model,
            "prompt": f"{system_prompt}\nUser: {prompt}\nJarvis:",
            "stream": False,
            "options": {"num_ctx": 512, "temperature": 0.7, "num_predict": 80},
        }
        try:
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                res = json.loads(resp.read().decode())
                return res.get("response", "").strip()
        except Exception as e:
            print(f"[LLM Exception: {e}]")
            return "Sorry, I am having trouble connecting to my brain right now."

    def run(self):
        """Continuous wake word detection and conversational loop."""
        print("\n" + "=" * 60)
        print("🎙️  HELLO JARVIS VOICE AGENT IS ACTIVE")
        print("=" * 60)
        print("Say: 'Hello Jarvis', 'Hey Jarvis', or 'Jarvis'")
        print("Or press Ctrl+C to exit.\n")

        WAKE_WORDS = [
            "hello jarvis", "hey jarvis", "hi jarvis", "jarvis",
            "hello", "hey", "hi", "service", "travis", "jan", "harvest",
            "javis", "garvis", "open jarvis"
        ]

        self.speak("Jarvis is online and at your service, sir.")
        notify_dashboard(is_listening=True, heard="", response="Jarvis is online and listening")

        while True:
            try:
                # 1. Listen for audio chunk to detect wake word (4.5s window, more sensitive threshold)
                audio = record_audio(max_seconds=4.5, silence_threshold=280, startup_silence=3.0)
                text = self.transcribe(audio)
                if not text:
                    continue

                lower_text = text.lower().strip()
                # Clean punctuation for matching
                clean_text = re.sub(r"[^\w\s]", "", lower_text)
                print(f"[Heard]: {text}")

                # Check if wake word or variation is present
                wake_detected = any(w in clean_text for w in WAKE_WORDS)

                if wake_detected:
                    print("\n✨ Wake word detected!")
                    notify_dashboard(is_listening=True, heard="Wake word detected...")
                    
                    # Check if the user already asked their question in the same breath!
                    remainder = clean_text
                    for w in sorted(WAKE_WORDS, key=len, reverse=True):
                        remainder = remainder.replace(w, " ").strip()
                    
                    words = remainder.split()
                    if len(words) >= 3:
                        # User already spoke the command in the same breath (e.g. "Hello Jarvis what can you do?")
                        cmd_text = text
                    else:
                        # Quick visual cue instead of potentially blocking audio sound
                        print("🎤 [Listening for command... speak now!]")
                        # subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"], check=False, capture_output=True)
                        cmd_audio = record_audio(max_seconds=12.0, silence_threshold=280, silence_seconds=1.8)
                        cmd_text = self.transcribe(cmd_audio)

                    if not cmd_text:
                        self.speak("I didn't quite catch that, sir.")
                        notify_dashboard(is_listening=True, heard="...", response="I didn't quite catch that, sir.")
                        continue

                    print(f"👤 You: {cmd_text}")
                    notify_dashboard(is_listening=True, heard=cmd_text)

                    # 3. Check for specific action commands (WhatsApp, etc.)
                    handled = self.handle_command(cmd_text)

                    # 4. If not a specific tool command, converse with LLM
                    if not handled:
                        response = self.ask_llm(cmd_text)
                        notify_dashboard(is_listening=False, heard=cmd_text, response=response)
                        self.speak(response)

            except KeyboardInterrupt:
                print("\n[Exiting Jarvis Voice Agent...]")
                notify_dashboard(is_listening=False, heard="", response="Voice agent offline")
                self.speak("Goodbye, sir.")
                break
            except Exception as e:
                print(f"[Loop error: {e}]")
                time.sleep(1)

if __name__ == "__main__":
    agent = VoiceAgent()
    agent.run()
