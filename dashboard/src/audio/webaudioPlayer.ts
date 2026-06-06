// Gapless PCM playback over the Web Audio API — schedule raw 16-bit signed LE
// mono chunks back-to-back as they arrive, for low time-to-first-audio. Shared
// by the one-shot platform-TTS replay (SoundIcon) and the streaming voice-mode
// sink. Whole int16 samples are carried across chunk boundaries (an odd trailing
// byte joins the next chunk).

type ACtor = typeof AudioContext

function audioContextCtor(): ACtor | null {
  const w = window as unknown as { AudioContext?: ACtor; webkitAudioContext?: ACtor }
  return w.AudioContext || w.webkitAudioContext || null
}

export function webAudioAvailable(): boolean {
  return typeof window !== 'undefined' && audioContextCtor() !== null
}

export interface PcmPlayer {
  /** Schedule one PCM chunk (s16le mono) to play after the previously queued audio. */
  enqueue(bytes: Uint8Array): void
  /** Stop immediately: halt all scheduled nodes and close the context. */
  stop(): void
  /** Resolves once every scheduled chunk has finished playing (or stop() ran). */
  drained(): Promise<void>
}

export function createPcmPlayer(sampleRate: number): PcmPlayer {
  const AC = audioContextCtor()
  if (!AC) throw new Error('Web Audio not available')
  const ctx = new AC()
  // A fresh AudioContext can start "suspended"; the user gesture that began
  // playback (icon/toggle click) is what unlocks it.
  void ctx.resume().catch(() => {})

  const live = new Set<AudioBufferSourceNode>()
  const ended: Promise<void>[] = []
  let nextStart = ctx.currentTime
  let carry = new Uint8Array(0)
  let stopped = false

  return {
    enqueue(bytes: Uint8Array) {
      if (stopped) return
      const merged = new Uint8Array(carry.length + bytes.length)
      merged.set(carry)
      merged.set(bytes, carry.length)
      const usable = merged.length - (merged.length % 2)
      carry = merged.slice(usable)
      if (usable === 0) return
      const pcm = new Int16Array(merged.buffer, 0, usable / 2)  // s16le (LE hardware)
      const f32 = new Float32Array(pcm.length)
      for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768
      const buf = ctx.createBuffer(1, f32.length, sampleRate)
      buf.copyToChannel(f32, 0)
      const src = ctx.createBufferSource()
      src.buffer = buf
      src.connect(ctx.destination)
      const startAt = Math.max(nextStart, ctx.currentTime)
      src.start(startAt)
      nextStart = startAt + buf.duration
      live.add(src)
      ended.push(new Promise<void>(resolve => { src.onended = () => { live.delete(src); resolve() } }))
    },

    stop() {
      stopped = true
      for (const s of live) { try { s.stop() } catch { /* already stopped */ } }
      live.clear()
      try { ctx.close() } catch { /* ignore */ }
    },

    async drained() {
      // Loop in case more chunks were still enqueuing when first awaited; settles
      // once the scheduled set stops growing.
      let n = -1
      while (ended.length !== n) {
        n = ended.length
        await Promise.all(ended)
      }
    },
  }
}
