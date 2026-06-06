// Platform STT — capture the mic, downsample to 16 kHz PCM, and stream it over
// /ws/audio/stt to the server STT provider. Used when the browser has no native
// Web Speech STT (Firefox/Safari) or the policy forces platform.
//
// The token is minted via POST /v1/audio/stt/session (cookie-authed) and sent in
// the first WS frame — never in the URL. Mic capture uses a
// ScriptProcessorNode: deprecated but supported everywhere, and avoids shipping a
// separate AudioWorklet module (AudioWorklet is a future upgrade).

import { apiFetch } from '../../api/auth'
import { type STTBackend, type STTSession, type STTCreateOptions } from '../types'

const TARGET_RATE = 16000

function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/audio/stt`
}

function downsampleToPCM16(input: Float32Array, srcRate: number, dstRate: number): ArrayBuffer {
  const ratio = srcRate / dstRate
  const outLen = Math.floor(input.length / ratio)
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const s = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out.buffer
}

export const platformStt: STTBackend = {
  kind: 'platform',

  isAvailable() {
    return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
  },

  create(opts: STTCreateOptions): STTSession {
    let ws: WebSocket | null = null
    let stream: MediaStream | null = null
    let ctx: AudioContext | null = null
    let node: ScriptProcessorNode | null = null
    let source: MediaStreamAudioSourceNode | null = null
    const handlers = {
      partial: (_: string) => {},
      final: (_: string) => {},
      error: (_: Error) => {},
      end: () => {},
    }

    const teardown = () => {
      try { node?.disconnect() } catch { /* ignore */ }
      try { source?.disconnect() } catch { /* ignore */ }
      try { ctx?.close() } catch { /* ignore */ }
      stream?.getTracks().forEach(t => t.stop())
      node = source = null; ctx = null; stream = null
    }

    return {
      async start() {
        // 1. Mint the short-lived token.
        const sessRes = await apiFetch('/v1/audio/stt/session', {
          method: 'POST',
          body: JSON.stringify({ provider_id: opts.providerId ?? null }),
        })
        if (!sessRes.ok) throw new Error(`Could not start STT session (${sessRes.status})`)
        const { ws_token } = await sessRes.json()

        // 2. Mic.
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        ctx = new AudioContext()
        // Autoplay policy can leave a fresh AudioContext "suspended"; the mic tap
        // is a user gesture, so resume it so audioprocess actually fires.
        await ctx.resume().catch(() => {})
        source = ctx.createMediaStreamSource(stream)
        node = ctx.createScriptProcessor(4096, 1, 1)

        // 3. WebSocket.
        ws = new WebSocket(wsUrl())
        ws.binaryType = 'arraybuffer'
        const sock = ws
        await new Promise<void>((resolve, reject) => {
          sock.onopen = () => {
            sock.send(JSON.stringify({
              type: 'init', token: ws_token, language: opts.language,
              sample_rate: TARGET_RATE, encoding: 'pcm_s16le',
            }))
          }
          sock.onerror = () => reject(new Error('STT socket error'))
          sock.onmessage = (ev) => {
            let msg: { type?: string; text?: string; message?: string }
            try { msg = JSON.parse(ev.data) } catch { return }
            if (msg.type === 'ready') {
              resolve()
            } else if (msg.type === 'final' && msg.text) {
              handlers.final(msg.text)
            } else if (msg.type === 'interim' && msg.text) {
              handlers.partial(msg.text)
            } else if (msg.type === 'error') {
              handlers.error(new Error(msg.message || 'STT error'))
            }
          }
          sock.onclose = () => { teardown(); handlers.end() }
        })

        // 4. Pump PCM once the server is ready.
        node.onaudioprocess = (e) => {
          if (sock.readyState !== WebSocket.OPEN || !ctx) return
          const pcm = downsampleToPCM16(e.inputBuffer.getChannelData(0), ctx.sampleRate, TARGET_RATE)
          sock.send(pcm)
        }
        source.connect(node)
        node.connect(ctx.destination)
      },

      async stop() {
        try {
          if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'stop' }))
        } catch { /* ignore */ }
        teardown()
        try { ws?.close() } catch { /* ignore */ }
        ws = null
      },

      onPartial(h) { handlers.partial = h },
      onFinal(h) { handlers.final = h },
      onError(h) { handlers.error = h },
      onEnd(h) { handlers.end = h },
    }
  },
}
