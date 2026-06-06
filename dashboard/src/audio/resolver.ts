// Pick the concrete backend from the server-resolved capability + the user's
// device mode. Mirrors the server's availability semantics.

import { type ChatAudioCapability, type AudioMode, type TTSBackend, type STTBackend } from './types'
import { nativeTts } from './backends/nativeTts'
import { platformTts } from './backends/platformTts'
import { nativeStt } from './backends/nativeStt'
import { platformStt } from './backends/platformStt'

export function resolveTtsBackend(cap: ChatAudioCapability, mode: AudioMode): TTSBackend | null {
  if (cap.tts === 'unavailable') return null
  if (cap.tts === 'native_only') return nativeTts.isAvailable() ? nativeTts : null
  if (cap.tts === 'platform') return platformTts
  // 'either' — user choice
  if (mode === 'native') return nativeTts.isAvailable() ? nativeTts : platformTts
  if (mode === 'platform') return platformTts
  return nativeTts.isAvailable() ? nativeTts : platformTts  // auto → prefer native
}

export function resolveSttBackend(cap: ChatAudioCapability, mode: AudioMode): STTBackend | null {
  const platform = platformStt.isAvailable() ? platformStt : null
  if (cap.stt === 'unavailable') return null
  if (cap.stt === 'native_only') return nativeStt.isAvailable() ? nativeStt : null
  if (cap.stt === 'platform') return platform
  // 'either' — user choice
  if (mode === 'native') return nativeStt.isAvailable() ? nativeStt : platform
  if (mode === 'platform') return platform
  return nativeStt.isAvailable() ? nativeStt : platform  // auto → prefer native
}
