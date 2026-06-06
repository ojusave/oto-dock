// Native TTS — Capacitor TextToSpeech on device, Web Speech (speechSynthesis)
// in the browser. Mirrors the unlock/speak approach already proven in
// useNotificationSound. createStream() adds the incremental voice-mode sink
// (speak sentences as a reply streams) on top of the one-shot play().

import { type TTSBackend, type TTSPlayOptions, type TtsStream, isNativePlatform, probeWithTimeout } from '../types'

function pickVoice(language: string, voiceURI?: string): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis?.getVoices?.() || []
  if (voiceURI) {
    const exact = voices.find(v => v.voiceURI === voiceURI)
    if (exact) return exact
  }
  return voices.find(v => v.lang.startsWith(language)) || voices.find(v => v.lang.startsWith('en')) || null
}

// Incremental sink: speak sentences in order as the reply streams. In the browser
// speechSynthesis has its OWN utterance queue, so each sentence is just spoken
// (no cancel() between them) — which also dodges Chrome's ~15 s single-utterance
// truncation. On device the Capacitor plugin speaks one clip at a time, so we
// drain a queue sequentially. cancel() = barge-in (stop + drop the queue).
function createNativeStream(opts: TTSPlayOptions): TtsStream {
  let cancelled = false
  let finished = false
  let settled = false
  let resolveDone!: () => void
  const done = new Promise<void>(r => { resolveDone = r })
  const settle = () => { if (!settled) { settled = true; resolveDone() } }

  if (isNativePlatform()) {
    const queue: string[] = []
    let draining = false
    const drain = async () => {
      if (draining) return
      draining = true
      try {
        const { TextToSpeech } = await import('@capacitor-community/text-to-speech')
        while (!cancelled && queue.length) {
          const text = queue.shift() as string
          try { await TextToSpeech.speak({ text, lang: opts.language, rate: 1.0 }) } catch { /* skip clip */ }
        }
      } finally {
        draining = false
        if (cancelled || (finished && queue.length === 0)) settle()
      }
    }
    return {
      push(text) { if (cancelled || finished || !text) return; queue.push(text); void drain() },
      finish() { if (cancelled || finished) return; finished = true; if (!draining && queue.length === 0) settle() },
      cancel() {
        if (cancelled) return
        cancelled = true; queue.length = 0
        import('@capacitor-community/text-to-speech').then(({ TextToSpeech }) => TextToSpeech.stop()).catch(() => {})
        settle()
      },
      done,
    }
  }

  // Browser: queue utterances on speechSynthesis (built-in FIFO ordering).
  const synth = typeof window !== 'undefined' ? window.speechSynthesis : undefined
  let outstanding = 0
  const maybeSettle = () => { if (finished && outstanding === 0) settle() }
  return {
    push(text) {
      if (cancelled || finished || !text || !synth) return
      const u = new SpeechSynthesisUtterance(text)
      u.lang = opts.language
      const v = pickVoice(opts.language, opts.voiceURI)
      if (v) u.voice = v
      outstanding++
      u.onend = () => { outstanding--; maybeSettle() }
      u.onerror = () => { outstanding--; maybeSettle() }
      synth.speak(u)
    },
    finish() { if (cancelled || finished) return; finished = true; maybeSettle() },
    cancel() {
      if (cancelled) return
      cancelled = true
      try { synth?.cancel() } catch { /* ignore */ }
      settle()
    },
    done,
  }
}

// ── Availability probe ────────────────────────────────────────────
// Same honesty rule as nativeStt: inside the APK, ask the TTS plugin for its
// voice list once instead of assuming Capacitor == speech — a device with no
// (enabled) TTS engine reports zero voices and every speak() fails. Memoized;
// useChatAudioCapability awaits it before the server capability resolve.

let probedNativeTts: boolean | null = null
let ttsProbeInFlight: Promise<boolean> | null = null

export async function probeNativeTtsAvailable(): Promise<boolean> {
  if (typeof window === 'undefined') return false
  if (probedNativeTts !== null) return probedNativeTts
  if (!isNativePlatform()) {
    probedNativeTts = 'speechSynthesis' in window
    return probedNativeTts
  }
  // A wedged TTS engine binding can hold getSupportedVoices() open forever —
  // answer false after a bounded wait; the still-running plugin call writes
  // the memo whenever the engine settles, so a later refetch gets the truth.
  ttsProbeInFlight ??= (async () => {
    try {
      const { TextToSpeech } = await import('@capacitor-community/text-to-speech')
      const { voices } = await TextToSpeech.getSupportedVoices()
      probedNativeTts = (voices || []).length > 0
    } catch {
      probedNativeTts = false
    }
    return probedNativeTts
  })()
  return probeWithTimeout(ttsProbeInFlight)
}

export const nativeTts: TTSBackend = {
  kind: 'native',

  isAvailable() {
    if (probedNativeTts !== null) return probedNativeTts
    return isNativePlatform() || (typeof window !== 'undefined' && 'speechSynthesis' in window)
  },

  async play(text: string, opts: TTSPlayOptions) {
    // Capacitor native first (reliable on Android).
    if (isNativePlatform()) {
      try {
        const { TextToSpeech } = await import('@capacitor-community/text-to-speech')
        const speakOpts: { text: string; lang: string; rate: number; voice?: number } = {
          text, lang: opts.language, rate: 1.0,
        }
        // The Capacitor plugin selects a voice by INDEX; map the chosen voiceURI.
        if (opts.voiceURI) {
          try {
            const { voices } = await TextToSpeech.getSupportedVoices()
            const idx = (voices || []).findIndex((v: { voiceURI?: string }) => v.voiceURI === opts.voiceURI)
            if (idx >= 0) speakOpts.voice = idx
          } catch { /* default voice */ }
        }
        await TextToSpeech.speak(speakOpts)
        return
      } catch { /* fall through to Web Speech */ }
    }
    if (!('speechSynthesis' in window)) throw new Error('No native TTS on this device')
    window.speechSynthesis.cancel()
    const u = new SpeechSynthesisUtterance(text)
    u.lang = opts.language
    const v = pickVoice(opts.language, opts.voiceURI)
    if (v) u.voice = v
    await new Promise<void>((resolve, reject) => {
      u.onend = () => resolve()
      u.onerror = () => reject(new Error('speech synthesis error'))
      window.speechSynthesis.speak(u)
    })
  },

  stop() {
    try { window.speechSynthesis?.cancel() } catch { /* ignore */ }
    if (isNativePlatform()) {
      import('@capacitor-community/text-to-speech')
        .then(({ TextToSpeech }) => TextToSpeech.stop())
        .catch(() => { /* ignore */ })
    }
  },

  createStream(opts: TTSPlayOptions) {
    return createNativeStream(opts)
  },
}

export interface VoiceOption { voiceURI: string; name: string; lang: string }

// The Android TextToSpeech plugin reports EVERY voice in a locale with the same
// display name ("German Germany") — only voiceURI (the engine voice name, e.g.
// "de-de-x-deg-local") is unique. Derive a distinct, human-ish label from it so
// the picker doesn't show N identical-looking options.
function nativeVoiceLabel(displayName: string, voiceURI: string): string {
  const m = voiceURI.match(/-x-([a-z0-9]+)-(local|network)\b/i)
  if (m) {
    const quality = m[2].toLowerCase() === 'network' ? 'online' : 'on-device'
    return `${displayName} · ${m[1].toLowerCase()} (${quality})`
  }
  // Fallback for non-Google engines: append the distinguishing tail of the URI.
  const tail = voiceURI.split(/[-_.\s]+/).filter(Boolean).slice(-2).join('-')
  return tail && !displayName.toLowerCase().includes(tail.toLowerCase())
    ? `${displayName} · ${tail}` : displayName
}

// Last-resort guard: if two voices still share a label, suffix #2, #3, … so the
// dropdown never shows ambiguous duplicates (selection is by voiceURI, so this
// is purely cosmetic and safe).
function ensureUniqueLabels(voices: VoiceOption[]): VoiceOption[] {
  const seen = new Map<string, number>()
  return voices.map(v => {
    const n = (seen.get(v.name) ?? 0) + 1
    seen.set(v.name, n)
    return n > 1 ? { ...v, name: `${v.name} #${n}` } : v
  })
}

/** Load the device's native voices for the prefs picker — unified across the
 *  browser (speechSynthesis) and Android (Capacitor TTS getSupportedVoices),
 *  both keyed on ``voiceURI``. Async because the native plugin call is. */
export async function loadVoices(): Promise<VoiceOption[]> {
  if (isNativePlatform()) {
    try {
      const { TextToSpeech } = await import('@capacitor-community/text-to-speech')
      const { voices } = await TextToSpeech.getSupportedVoices()
      const mapped = (voices || [])
        .filter((v: { voiceURI?: string }) => !!v.voiceURI)
        .map((v: { voiceURI: string; name?: string; lang?: string }) => ({
          voiceURI: v.voiceURI,
          name: nativeVoiceLabel(v.name || v.voiceURI, v.voiceURI),
          lang: v.lang || '',
        }))
      return ensureUniqueLabels(mapped)
    } catch { return [] }
  }
  // Browser voices already carry real, distinct names ("Google Deutsch", …).
  return (window.speechSynthesis?.getVoices?.() || []).map(v => ({
    voiceURI: v.voiceURI, name: v.name, lang: v.lang,
  }))
}
